# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/events.py`：巨灾事件台账的事件汇总、余额计算和容量状态评估（纯函数）。
- `src/repository.py`：SQLite建表、事务和查询（事件与赔案清单持久化）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `static/event.html`：巨灾事件台账详情页（赔案清单、未结金额、合约层余额、补证）。
- `tests/`：完整流程、规则计算、事件台账和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/events`：事件通知台账列表（含汇总）。
- `POST /api/events`：首次报案登记，请求体为`{"event_id":"...","occurred_at":"...","estimated_total_loss":0,"reported_by":"..."}`，报送人缺省取`X-User-Id`。
- `GET /api/events/{event_id}`：事件详情，含赔案清单、未结金额和合约层容量/已占用/余额。
- `POST /api/events/{event_id}/claims`：把已有赔案手动追加到事件，请求体为`{"record_id":1,"claim_number":"...","estimated_loss":0}`（后两项可缺省）。
- `POST /api/events/{event_id}/supplement`：补证下调预估总损失，请求体为`{"estimated_total_loss":0}`，必须低于当前值。
- `GET /event`：事件台账详情页，可带`?event_id=...`。

除`/health`、`/`和`/event`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。事件报案、追加赔案和补证限`claims_officer`（`admin`可代办），查询对全部已知角色开放。

## 巨灾事件通知台账

- 第一次报案登记事件的发生时刻、预估总损失和报送人；后续赔案追加到同一事件，再保团队可跨合约查看同一事件的损失进展。
- 报案有两种方式：直接调用`POST /api/events`登记；或在`submit_claim`动作的`data`中携带`occurred_at`和`estimated_total_loss`，首次报案自动登记，之后的提赔自动追加（幂等）。
- 合约层容量 = `layer_width × cession_pct`。事件预估总额超过合约层容量时，追加的赔案标记为`pending_evidence`（待补证），`calculate`核定动作被拒绝。
- 补证将预估总损失下调到容量内后，待补证赔案自动恢复为`admitted`，可继续核定；已经`settled`/`rejected`的赔案保持原样，不回改金额与状态。
- 事件、赔案清单和状态均落库，重启后可按事件查看未结金额（未结算赔案摊回金额合计）和赔案清单。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及事件台账的报案登记、待补证/补证恢复、已结算赔案保持原样和重启持久化。
