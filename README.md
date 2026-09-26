# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

访问 <http://127.0.0.1:8101>。默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `GET /api/papers/{id}/conflicts`：冲突档案（含已撤回记录与撤回说明），仅主席可见。
- `POST /api/papers/{id}/conflicts/withdraw`：主席撤回误登记的冲突，需填写说明。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

## 冲突撤回规则

冲突档案（`conflicts` 状态列 + `conflict_withdrawals` 表）、撤回规则（`ReviewStore.withdraw_conflict` 及 `assign` 中的重新表达意愿检查）、页面入口（`web/conflicts.html`，路径 `/conflicts`）三部分分开维护。

- 仅主席可撤回，必须填写说明；已撤回的记录不能重复撤回。
- 撤回后原冲突记录转入 `withdrawn` 状态，登记人/时间与撤回人/时间都保留在档案中。
- 论文仍在评审中时：未完成的邀请（未应答或已接受未提交评审）同步取消；已完成评审保留在时间线，但状态作废（`voided`），不再计入 Rebuttal 门槛与决定所需评审数，也不能用于补位。
- 撤回后评审人需重新表达评审意向（`want`/`maybe`），主席才能再次邀请；再次邀请会创建新的分配记录。
- 论文已决定或已撤稿时，撤回只记录处置（档案 + 审计），不改变结论，也不改动任何分配。
- 撤回后如再次发现真实冲突，可重新登记，历史撤回记录仍保留在档案中。

## 业务不变量

评审人不能查看未分配论文的作者身份；生效中的利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审；冲突撤回后评审人需重新表达意愿才能再次被邀请，已作废评审不计入决定。
