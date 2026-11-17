# 故障复盘：<一句话标题>

- 事故编号：<INC-xxxx / 无>
- 环境：<prod / prod-gray / staging / test>
- 开始 / 恢复：<YYYY-MM-DD HH:MM> ~ <YYYY-MM-DD HH:MM>（CST）
- 影响时长：<xx 分钟>
- 严重级别：<P0/P1/P2/P3>
- 负责人：<name>

## 1. 影响面

| 维度 | 内容 | 证据 |
|---|---|---|
| 服务 / 接口 | <svc / uri> | <PromQL> |
| 租户 / 地区 | <tenant / region> | <query> |
| 错误率 / P99 | <x% / xx ms> | <query + window> |
| 失败请求量 | <n> | <query> |
| 用户可见表现 | <现象> | <截图/日志> |

## 2. 时间线

| 时间 (CST) | 事件 | 来源 |
|---|---|---|
| HH:MM | 首个告警触发 | <alertname> |
| HH:MM | 发布 / 配置变更 | <release order / commit> |
| HH:MM | 指标开始异常 | <query> |
| HH:MM | 处置动作 | <who / what> |
| HH:MM | 恢复确认 | <query> |

## 3. 证据

### 3.1 指标

```promql
<query>
```

- 窗口：<start ~ end，step>
- 结果：<数值/曲线摘要>
- 覆盖：<OK / 部分 / UNKNOWN，原因>

### 3.2 日志

```logsql
<query>
```

- 聚合：<top patterns + counts>
- 样本：<trace_id / pod / 时间>

### 3.3 Trace

- trace_id：<id>
- 慢点 / 错误点：<service / span / duration>
- 区分 mesh 开销与业务耗时：<结论 + 证据>

### 3.4 变更与配置

- 发布单：<id / name / submitted_at / approval>
- 镜像：<repo:tag / digest>
- GitOps / ConfigMap：<path / 关键 diff>

## 4. 根因分析

### 4.1 主假设

<一句话>

### 4.2 支持证据

- <evidence>

### 4.3 反证与排除

- 反证：<本应出现但未出现的现象>
- 排除：<其他候选原因 + 为什么排除>

### 4.4 结论

<确定性：已确认 / 高度可能 / UNKNOWN；边界条件>

## 5. 处置与恢复

| 时间 | 动作 | 执行人 | 结果 |
|---|---|---|---|
| HH:MM | <action> | <name> | <result> |

## 6. 改进项

| 类型 | 改进项 | Owner | 截止 | 验证方式 |
|---|---|---|---|---|
| 监控 | <新增/调整告警> | <name> | <date> | <query / 演练> |
| 容量 | <扩容/限流> | <name> | <date> | <指标> |
| 代码 | <修复/防重入> | <name> | <date> | <测试/发布验证> |
| 流程 | <门禁/审批> | <name> | <date> | <检查项> |

## 7. 数据缺口

- <UNKNOWN 项 + 为什么拿不到 + 后续补法>