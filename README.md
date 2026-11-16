# 车队部件寿命估计

寿命建模的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 按站下发检修指令（冻结型号/里程/版本、逐站核对回执、撤销与对账）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划、Station 检修站点、StationDirective 站点指令；辅助 User、Audit、Delivery。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18216；后端文档 http://localhost:19216/docs；数据库宿主端口 17716。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、按站下发与撤销、对账、站端演练、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准、下发、撤销或对账|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。站点业务检查可在临时 SQLite 上执行 `python -m scripts.test_stations`（内嵌站端模拟器，不依赖 Redis 或外部站点）。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 各站检修指令（06 / STATION CONTROL）
已批准结果按站点下发，每份指令冻结目标型号、目标里程、目标可靠度与版本号（站点内递增，v1、v2…），并带唯一幂等键。页面 `#/stations` 提供下发、撤销、对账与两侧生效版本对照，每张站卡都可打开指令记录。

- 回执：站端对每条指令持久化回执台账，后端确认后才记生效；同批结果未核对前不会重复下发，已生效的同批结果也不会再下发。
- 漏回执：站端已生效但回执丢失时，指令保持“回执丢失待查”；重试复用原指令与幂等键，站端从台账回答原结果，不重复生效。对账同样只查询站端台账与当前生效版本并补齐回执。
- 撤销：撤销针对明确版本，站端确认前页面显示“撤销待核对”，后端不把版本记为已撤销；确认后才清空生效版本。旧版本的迟到撤销不会撤掉新版。
- 乱序：旧版本的下发或撤销迟到时，站端按版本与已撤销水位判定，不会覆盖或撤掉更新版本；后端把该指令记为“已被取代”。
- 中断与隔离：worker 启动恢复半开的未决指令，按逐条提交推进；离线站点保持待下发重试，不阻塞其他站点。
- 站端演练台：可切换站点在线/离线、模拟漏回执、暂扣指令后按新到先发或先到先发释放，用于演练漏回执与乱序指令。

接口：`GET /stations/board` 两侧版本与待决指令；`POST /stations/{id}/publish`、`POST /stations/{id}/revoke`、`POST /stations/{id}/reconcile`、`POST /stations/reconcile`、`POST /stations/{id}/directives/{directive}/retry`。站端侧：`GET /stations/{id}/station`、`POST /stations/{id}/station/drill`、`POST /stations/{id}/station/release`、`GET /stations/{id}/station/directives/{directive}`（只读查回原回执）。下发、撤销、对账与演练开关仅业务负责人可用，其余角色只读。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=fleet-weibull；FRONTEND_PORT=18216；BACKEND_PORT=19216；DB_PORT=17716；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1017/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
