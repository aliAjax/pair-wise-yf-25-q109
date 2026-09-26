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
- `GET /api/papers/{id}/conflicts`：主席查看冲突档案（含已撤回记录、双方操作者与时间）。
- `POST /api/papers/{id}/conflicts/withdraw`：主席填写说明撤回利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。

## 冲突撤回

冲突档案（`GET /api/papers/{id}/conflicts` 与 `ReviewStore.list_conflicts`）、撤回规则（`ReviewStore.withdraw_conflict` 及 `assign`/`decide` 中的联动检查）、页面入口（`web/index.html` 的冲突面板）分开维护。撤回规则：

- 主席必须填写撤回说明；原记录转入 `withdrawn`，登记与撤回双方的操作者、时间、说明都保留在档案中。
- 撤回与处置在同一事务完成：未处理邀请同步取消（`cancelled`，不可再响应）；已完成意见留在时间线里，但标记 `excluded`，不再计入补位与决定采用。
- 撤回后评审人需重新表达意愿（撤回时间之后的 `want`/`maybe` 意向），主席才可再次邀请；再次邀请生成新的分配记录，历史记录保留。
- 论文已经决定时，撤回只记录处置（档案与审计），不改变结论。
- 已撤回的冲突可重新登记，重新生效。
