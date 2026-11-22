"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import partition_rule, resource, cross_module

# 固定路径路由必须先于通用 /api/{module} 资源路由注册。
ROUTERS = [partition_rule, resource, cross_module]