# Metadata DeepSearch：真实困难样本验证与完整实施方案

日期：2026-09-06。状态：**固定 8 场景的 A/B/C 验证已完成；安全增益不足，不启动整体重构。**

用户要求先从既有生产样本中验证兜底增益，再决定重构。本目录交付固定样本、冻结证据、现有管线基线、真实模型尝试、独立证据审查和后续完整方案。没有修改生产 metadata 服务、生产数据库或 corpus 的 confirmed 答案。

## 当前结论

- 从原 1,372 条快照挑选 8 个真实困难/对照场景，共 14 个研究字段。16 次 wigolo 搜索，尝试读取 40 页，14 页获得非空正文。
- 用户恢复模型后，原流程 `process_title_only` 8 个基线已全部重跑，无外层执行异常；48 个 B/C 三轮配对请求全部正常返回，覆盖 84 个字段回答。
- 两组均只有 1 个去重字段（四谎 OAD 集数）三轮通过严格证据复核。正文组未新增可接受字段，输入 token 却为摘要组的 3.70 倍。
- 发现真实风险：Zeztz 五次把第三方总数说成官方确定值；Shazam 正文组三次误绑定相邻国家的表格行。现有引用存在性校验不足以拦截。
- 原流程恢复后还暴露 Ultraman 错匹配动画作品的问题；仅在 `found=false` 时触发兜底会漏掉这种错误。下一步应验证身份矛盾检测、来源筛选、结构化引用和定向追加取证，而非直接整体重构。
- 初始推理预算试验、NCCL/NVML 故障批次原样保留，不与恢复后的结果混算。最新完整结论见 [RECOVERY.md](RECOVERY.md)，历史故障与初始探索见 [RESULTS.md](RESULTS.md)。

## 导航

| 文件/目录 | 用途 |
|---|---|
| [PROTOCOL.md](PROTOCOL.md) | 比较组、计分口径、防泄漏与重构准入条件 |
| [RECOVERY.md](RECOVERY.md) | 最新：完整三轮结果、逐字段证据审查与 NO-GO 决策 |
| [REFACTOR.md](REFACTOR.md) | 已实施：仅重构现有兜底证据边界，含确定性旧/新对照与回归结果 |
| [RESULTS.md](RESULTS.md) | 历史：初始逐场景发现、故障与限制 |
| [PLAN.md](PLAN.md) | 缺口驱动 DeepSearch 的完整设计、分期实施、验收与回滚 |
| [cases.json](cases.json) | 固定 UUID、原始标题、场景、字段、问题和原 torrent 清单 |
| [oracles.json](oracles.json) | 独立审查者盲看证据形成的临时 oracle；不是生产金标 |
| [summary.json](summary.json) | 由脚本生成的执行统计，不自动宣告事实准确率 |
| `evidence/*.json.gz` | wigolo 摘要、搜索错误/警告、页面正文/读取错误、采集时间和内容 hash |
| `baseline/*.json` | 现有 title-only 管线原始结果；可用的 HTTP 录制在同目录 |
| `runs/*.json` | 初始推理预算试验，保留成功与失败，不覆写 |
| `runs-json/*.json` | 服务故障期间的第二次尝试，不计为质量样本 |
| `runs-recovery-v1/*.json` | 恢复后的完整 48 次配对请求，不覆盖历史结果 |
| `recovery-baseline/baseline/*` | 恢复后的 8 个基线结果与 HTTP 录制 |

所有 `auto_apply_safe` 都是本次证据审查的建议，不代表系统已经具有该安全判定能力或获得人工批准。

## 复现与续跑

离线核验（不需要 LLM，不连接生产库）：

```bash
.venv/bin/python scripts/metadata_deepsearch_summary.py
.venv/bin/pytest tests/integration/metadata_corpus/test_deepsearch_eval.py -q
```

用户已恢复共享模型服务，下列命令对应本次完成的实验。本轮没有执行服务重启。需要重新实验时使用另一个新名字，保留全部历史证据：

```bash
.venv/bin/python scripts/metadata_deepsearch_eval.py judge --online \
  --llm-base-url http://127.0.0.1:8000/v1 --run-name runs-recovery-v1
.venv/bin/python scripts/metadata_deepsearch_summary.py
```

`judge` 只读取冻结搜索/页面，外部联网仅为模型调用。默认两组各三轮、并发 2，单请求 90 秒、4096 输出 token、JSON mode 和 `enable_thinking=false`；新服务必须先确认兼容这些参数。模型配置变化必须作为新实验记录，不能与旧组混算。

续跑会校验输入/提示词/证据/模型配置指纹，不兼容、损坏或失败的已有记录会明确报错，而不是静默跳过。初始归档未包含全部新增指纹，保持原样留作历史证据，不补写伪造的来源信息；请勿在原归档目录重跑 `collect` 或 `baseline`。新 `judge` 实验可以直接读取这些冻结证据，其结果会记录证据文件 hash。

若需要重新搜索，使用新的目录版本（`extract --output ...` → `collect --output ... --online`），保留现有证据。原流程 A 对服务故障样本的重跑也使用新目录；基线成功字段需与研究字段做语义对齐，不能拿全球最早发行日期与美国公映日期直接判对错。

完整验收能力已进入 `tests/integration/metadata_corpus/` 和现有 Fast/Strict/发布门禁；本实验新增的护栏测试同样离线运行。真实搜索及真实 LLM 对比仍显式执行，不使 CI 依赖外部服务。
