# LLM 配置说明

> 这套系统的 AI 功能（复盘/踩坑/沉淀知识/月度体检/看板）都靠一个 LLM API。
> 任何**兼容 OpenAI 接口**的提供商都能用（DeepSeek / OpenAI / 通义千问 / Kimi / 智谱 GLM / 商汤 SenseNova 等）。
> 纯标准库调用，不需要装任何 pip 包。

## 三步配好

### 1. 复制配置模板
```bash
cd tools/
cp config.env.example .env
```

### 2. 编辑 .env，填你的密钥和提供商
打开 `tools/.env`，至少填这三个：
```
LLM_API_KEY=sk-你的真实密钥
LLM_BASE_URL=https://api.deepseek.com/v1   # 换成你用的提供商
LLM_MODEL=deepseek-chat                    # 跟着提供商填
LLM_TEMPERATURE=0.4                        # 0.3 稳健，0.7 发散
```

常见提供商 `base_url`（取一个用）：
| 提供商 | base_url | 示例 model |
|---|---|---|
| DeepSeek（便宜，推荐日常复盘） | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| 阿里通义千问（兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| 商汤 SenseNova（OpenAI 兼容端点） | 见其控制台 | 见其模型列表 |

> 只要提供商支持 `POST {base_url}/chat/completions`、Bearer 鉴权、OpenAI 格式的 messages，就能用。

### 3. 测试连通
```bash
python tools/assistant.py test
```
看到「✅ API 连通，模型回复：通」就成了。

## 多模型管理（推荐：用看板）

如果你想同时存多个模型、随时切换（比如日常用便宜的、复杂复盘用强的），**不用手改 .env**：

1. 启动看板：`python tools/server.py`
2. 点右上角「⚙ 模型」→ 添加模型（填 label / model / base_url / api_key / temperature）
3. 点某个模型「设为当前」即切换

模型存在 `tools/models.json`（已 gitignore，密钥不外传）。`.env` 只是第一次的种子/兜底。
命令行和看板**共用同一个切换**——看板里切了，命令行也跟着切。

## 两种入口怎么调 LLM

- **命令行**（`assistant.py review` 等）：读 `tools/models.json` 里当前 active 的模型；没有就回退读 `.env`。
- **看板**（`server.py`）：同上，统一走 `assistant.load_config()` 这一个入口。
- 两者**从不把原始密钥发给浏览器**——API 只返回掩码后的密钥（`••••` + 后 4 位）。

## 安全（务必读）

- `.env` 和 `tools/models.json` 都含真实密钥，**已在 `.gitignore` 里忽略，永远不会进 git**。
- **永远不要把 `.env` / `models.json` 发给别人或贴到聊天里。**
- 要分享/开源这个项目时，只分享 `config.env.example`（模板，无密钥）。
- AI 助手**不会自动改规则库**——提炼出的规则先进 `规则库_候选.md`，你确认后再手动搬进 `规则库.md`。人保留最终决定权。

## 不想用 AI 也行

这套系统的核心是**文件结构 + 闭环方法论**，AI 只是省力。你可以纯手动用（见 [系统说明](系统说明.md) 的"用法 A"），每周手写复盘一样能跑。
