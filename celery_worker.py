"""用于运行 celery worker，单独创建一个文件是为了提供上下文环境"""

from ubattery import create_app
# 导出给 celery 使用
from ubattery.extensions import celery

app = create_app()
app.app_context().push()