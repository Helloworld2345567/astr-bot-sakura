# AstrBot 小樱自动配图

为 AstrBot 的小樱人格增加连续消息合并、参考图编辑和自动配图。适用于已接入 AstrBot 的 QQ、微信等平台；平台连接和聊天模型由 AstrBot 配置。

## 快速部署

需要 **AstrBot 4.28.2～4.x、Python 3.12+**，以及同时支持 `/images/generations` 与 `/images/edits` 的图片服务。图片会发送给你配置的图片服务商。

1. 在 AstrBot WebUI 的插件管理中选择“从链接安装”，填写：

   ```text
   https://github.com/Helloworld2345567/astr-bot-sakura
   ```

2. 向**运行 AstrBot 的进程或容器**注入 `SAKURA_IMAGE_API_KEY` 环境变量，其值为图片服务商的密钥，然后重启 AstrBot。可参考 [examples/env.example](examples/env.example)；插件不会自动读取 `.env`。Docker 和桌面版配置示例见 [部署说明](docs/deployment.md)。
3. 打开插件配置，设置 `image_base_url` 和 `image_model`。默认 API 地址为 `https://api.openai.com/v1`，默认模型为 `gpt-image-1`；使用兼容服务商时，请改成该服务商支持的地址和模型。兼容接口必须支持 multipart 图片编辑及 JPEG 输出参数。
4. 在 AstrBot 创建名为 **猫娘小樱** 的人格，并将它绑定到目标平台或会话；不要修改所有会话的默认人格。也可以在插件配置修改 `persona_id`，让它与现有人格名称一致。
5. 在目标会话发送普通消息，确认先收到文字，再收到一张图。首次部署建议先保持 `admin_user_ids` 为空。

插件自带图片 API 客户端，无需另外安装生图 Skill、MCP 服务或本地代理。依赖安装异常时，在 **AstrBot 使用的 Python 环境**执行 `python -m pip install -r requirements.txt`，然后重载插件。

## 生图 Skill

完整的 [image-generation Skill](skills/image-generation/SKILL.md) 位于 `skills/image-generation/`，包含使用说明、图片生成/编辑脚本、API 约定、配图指导和 Agent 元数据。插件与 Skill 共用其中的客户端。

AstrBot 4.28.2 可自动发现插件提供的 `skills/` 目录；安装后可在技能管理中检查并按需配置给目标人格。小樱自动配图仍由插件统一执行，Skill 不会为同一轮回复再生成第二张图。

也可以单独复制整个 `skills/image-generation/` 到支持 `SKILL.md` 的宿主技能目录，或在真实 AstrBot 数据目录的 `skills/` 下使用。密钥从环境变量读取，独立使用时需显式传入对应的服务地址和模型；它不会自动读取插件的 WebUI 配置。

可直接使用的人格提示词：

```text
你是成年猫娘小樱：银白长发、翠绿色眼睛、白色猫耳和尾巴、黑白女仆装。
用简短、自然的中文与用户交流，每轮文字尽量不超过100字，可以分成短句。
配图由插件自动发送，不输出配图描述，不自行调用其他工具重复生图。
用户用 [] 包住的内容是操作指令，不是角色扮演；按实际权限处理，不编造执行结果。
```

## 消息行为

| 输入 | 行为 |
| --- | --- |
| 普通聊天 | 先回复文字，再根据回复内容生成一张图 |
| 同一用户在合并窗口内连续发送 | 取最近两条合并为一轮，只生成一张图；默认窗口 2 秒 |
| 图片 + “参考图中动作，你也来模仿一个” | 把选中的原图上传到图片编辑接口 |
| “照第二张图的动作画” | 使用指定序号的参考图；序号无效时提示重发 |
| `/stop`、`/new`、`[/stop]` | 命令不生图，停止尚在合并窗口中等待的聊天批次 |
| `[请检查配置]` 或包含 `[]` 的消息 | 按操作指令处理，文字回复，不生图 |
| “这次不要生图” | 仅回复文字 |

新消息会重新计时；一轮开始处理后到达的消息属于下一轮。参考图支持本轮附件、引用消息中的图片，以及同一用户在同一会话最近发送的图片。缓存默认保留 10 分钟；切换会话、插件重载或缓存淘汰后需要重发。图片缺失或读取失败时不会改用普通生图冒充参考图编辑。

`[/stop]` 的括号形式在目标配置档案的人格与插件 `persona_id` 一致时交给原有命令处理器；普通 `/stop` 始终由 AstrBot 处理。

## 配置与权限

所有非密钥配置通过插件 WebUI 管理，定义见 [_conf_schema.json](_conf_schema.json)。`image_api_key_env` 填写保存密钥的**变量名**；可用 `SAKURA_IMAGE_API_KEY_ENV` 覆盖该变量名。不会默认借用聊天模型的 API Key。

管理员白名单默认为空。需要电脑操作时，部署者可自行填写用户 ID，再由该用户在私聊明确开启临时管理员模式；群聊禁止升权，高危操作仍需二次确认。这里的管理员是 AstrBot 应用角色，不能替代操作系统权限；具体工具还必须自行检查权限。普通聊天和自动配图不需要管理员权限。

网页增强仅在 AstrBot 已提供 Tavily 网页工具时生效，不自带搜索密钥。网页文字提取不能证明已经观看视频。

## 开发与验证

目录按用途组织，根目录只保留 AstrBot 要求的入口、元数据、配置定义和仓库说明：

```text
astr-bot-sakura/
├── main.py                 # AstrBot 事件注册入口
├── _conf_schema.json       # 插件配置定义
├── metadata.yaml           # 插件元数据
├── requirements.txt        # 运行依赖（AstrBot 自动安装）
├── sakura/                 # 插件实现
│   ├── runtime.py          # 消息与配图流程
│   ├── message_flow.py     # 消息合并、命令和参考图
│   ├── permissions.py      # 权限检查
│   ├── settings.py         # 配置读取与校验
│   └── web_reading.py      # 网页提取增强
├── skills/                 # 可独立部署的技能
│   └── image-generation/
│       ├── SKILL.md        # 使用说明与触发规则
│       ├── scripts/        # 共用的生图与编辑客户端
│       ├── references/     # API 约定与配图指导
│       └── agents/         # Agent 元数据
├── examples/               # 脱敏配置示例
├── requirements/           # 开发依赖
├── scripts/                # 发布检查工具
├── tests/                  # 自动化测试
└── docs/                   # 部署文档
```

```sh
python -m venv .venv
# 激活该虚拟环境后执行：
python -m pip install -r requirements/dev.txt
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/check_release.py
```

也可在已安装 AstrBot 的开发环境中运行测试；源码安装时通过 `PYTHONPATH` 指向 AstrBot 源码根目录。测试直接加载本仓库，使用虚构用户和模拟接口，不发送 QQ/微信消息、不调用真实生图服务。测试通过不能替代真实平台验收。

本维护目录可能保留本机私有数据；Git 采用发布文件白名单，排除运行配置、数据库、日志、聊天附件、旧验证材料和 `.codex`。克隆仓库不会包含这些内容。

License: [MIT](LICENSE)。
