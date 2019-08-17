import time
from datetime import datetime
from typing import Dict, List

from flask import request, abort
from flask.views import MethodView

from ubattery import json_response
from ubattery.models import MYSQL_NAME_TO_TABLE
from ubattery.checker import RE_DATETIME_CHECKER
from ubattery.extensions import celery, mongo, mysql, cache
from ubattery.permission import permission_required
from ubattery.status_code import INTERNAL_SERVER_ERROR
from .algorithm import compute_battery_statistic
from .algorithm import compute_charging_process
from .algorithm import compute_working_condition


# 如果你不能马上使用 Celery 实例，用 `shared_task` 代替 task，如 Django 中。
# `ignore_result=True` 该任务不会将结果保存在 redis，提高性能
@celery.task(bind=True, ignore_result=True)
def compute_task(self,
                 task_name: str,
                 data_come_from: str,
                 request_params: str,
                 create_time: str,
                 # 这三个参数传给 SQl 语句
                 table_name: str,
                 start_date: str,
                 end_date: str) -> None:
    """根据 task_name_chinese，选择任务交给 celery 执行。

    :param self: Celery 装饰器中添加 `bind=True` 参数。告诉 Celery 发送一个 self 参数到该函数，
                 可以获取一些任务信息，或更新用 `self.update_stat()` 任务状态。
    :param task_name: 任务名，中文。
    :param data_come_from: 数据来源的中文名称，用于入库。
    :param request_params: 请求的参数，用于入库
    :param create_time: 任务执行的时间，从外部传入，保持一致性。
    :param table_name: 从哪张表查询数据，表名。
    :param start_date: 数据查询起始日期，>=。
    :param end_date: 数据查询终止日期，<=。
    """

    # 用 celery 产生的 id 做 mongo 主键
    task_id = self.request.id

    if task_name == '充电过程':
        need_params = 'bty_t_vol, bty_t_curr, battery_soc, id, byt_ma_sys_state'
        compute_alg = compute_charging_process
    elif task_name == '工况':
        need_params = 'timestamp, bty_t_curr, met_spd'
        compute_alg = compute_working_condition
    elif task_name == '电池统计':
        need_params = 'max_t_s_b_num, min_t_s_b_num'
        compute_alg = compute_battery_statistic
    else:
        return

    start = time.perf_counter()

    mongo.db['mining_tasks'].insert_one({
        '_id': task_id,
        'taskName': task_name,
        'dataComeFrom': data_come_from,
        'requestParams': request_params,
        'createTime': create_time,
        'taskStatus': '执行中',
        'comment': None,
        'data': None
    })

    if start_date is None:
        rows = mysql.session.execute(
            'SELECT '
            f'{need_params} '
            f'FROM {table_name}'
        )
    else:
        rows = mysql.session.execute(
            'SELECT '
            f'{need_params} '
            f'FROM {table_name} '
            'WHERE timestamp >= :start_date and timestamp <= :end_date',
            {'start_date': start_date, 'end_date': end_date}
        )

    if rows.rowcount == 0:
        mongo.db['mining_tasks'].update_one(
            {'_id': task_id},
            {'$set': {
                'taskStatus': '失败',
                'comment': '无可用数据',
            }}
        )
        return

    # 处理数据
    rows = [dict(row) for row in rows]
    data = compute_alg(rows)

    used_time = round(time.perf_counter() - start, 2)

    mongo.db['mining_tasks'].update_one(
        {'_id': task_id},
        {'$set': {
            'taskStatus': '完成',
            'comment': f'用时 {used_time}s',
            'data': data
        }}
    )


def get_task_list() -> List[Dict]:
    """这个函数不太好用缓存，因为会频繁创建任务。"""

    data = []
    for item in mongo.db['mining_tasks'].find(projection={'data': False}):
        # 修改传出的字段名
        item['taskId'] = item.pop('_id')
        data.append(item)
    data.reverse()
    return data


@cache.memoize()
def get_task(task_id: str) -> List[Dict]:
    """获取单个任务数据"""

    return mongo.db['mining_tasks'].find_one(
        {'_id': task_id},
        projection={'_id': False, 'data': True}
    )['data']


class MiningTasksAPI(MethodView):

    decorators = [permission_required()]

    def get(self, task_id):
        """返回任务。"""

        # 获取所有任务
        if task_id is None:
            data = get_task_list()
            return json_response.build(data=data)

        # 获取指定任务
        data = get_task(task_id)
        if data is None:
            cache.delete_memoized(get_task, task_id)
            return json_response.build(code=json_response.ERROR, msg='无可绘制数据！')

        return json_response.build(data=data)

    def post(self, task_name):
        """创建任务。"""

        jd = request.get_json()
        data_come_from = jd.get('dataComeFrom')
        if data_come_from not in MYSQL_NAME_TO_TABLE:
            abort(INTERNAL_SERVER_ERROR)
        table_name, _ = MYSQL_NAME_TO_TABLE[data_come_from]
        all_data = jd.get('allData')
        start_date = None
        end_date = None

        if all_data is None:
            start_date = jd.get('startDate')
            if start_date is None or not RE_DATETIME_CHECKER.match(start_date):
                abort(INTERNAL_SERVER_ERROR)
            end_date = jd.get('endDate')
            if end_date is None or not RE_DATETIME_CHECKER.match(end_date):
                abort(INTERNAL_SERVER_ERROR)
            request_params = f'{start_date} - {end_date}'
        else:
            request_params = '所有数据'

        create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if task_name == 'charging-process':
            task_name_chinese = '充电过程'
        # elif task_name == 'working-condition':
        #     task_name = '工况'
        elif task_name == 'battery-statistic':
            task_name_chinese = '电池统计'
        else:
            return json_response.build(code=json_response.ERROR)

        # 交给 celery 计算
        # 返回一个 task，可以拿到任务 Id 等属性
        task = compute_task.delay(
            task_name_chinese, data_come_from, request_params, create_time,
            table_name, start_date, end_date,
        )

        data = {
            'taskName': task_name_chinese,
            'dataComeFrom': data_come_from,
            'requestParams': request_params,
            'taskId': task.id,
            'createTime': create_time,
            'taskStatus': '执行中',
            'comment': None,
        }
        return json_response.build(data=data)

    def delete(self, task_id):
        # 取消一个任务，
        # 如果该任务已执行，那么必须设置 `terminate=True` 才能终止它
        # 如果该任务不存在，也不会报错
        compute_task.AsyncResult(task_id).revoke(terminate=True)

        mongo.db['mining_tasks'].delete_one({'_id': task_id})
        return json_response.build()