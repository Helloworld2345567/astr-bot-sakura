# 部署与排障

## Docker

在现有 AstrBot Compose 服务中添加环境变量，不改变你已有的镜像、卷或平台配置：

```yaml
services:
  astrbot:
    environment:
      SAKURA_IMAGE_API_KEY: ${SAKURA_IMAGE_API_KEY:?Set the image provider key}
```

把 `examples/env.example` 复制到 **Compose 文件所在目录** 的 `.env`，在本机填写密钥，然后执行 `docker compose up -d`。Compose 文件的服务名应与你实际部署的一致。不要提交 `.env`。

在 AstrBot 插件管理中从仓库链接安装本插件，填写图片服务地址、模型并绑定人格。容器必须能够访问图片 API、平台服务及附件下载地址。

## Windows / AstrBot 桌面版

在 PowerShell 7 中设置当前用户环境变量，输入不会回显：

```powershell
$imageKey = Read-Host '图片服务 API Key' -MaskInput
[Environment]::SetEnvironmentVariable('SAKURA_IMAGE_API_KEY', $imageKey, 'User')
Remove-Variable imageKey
```

完整退出并重新启动 AstrBot，使其继承更新后的环境变量。若桌面启动器仍使用旧环境，注销后重新登录。也可在同一个 PowerShell 会话设置 `$env:SAKURA_IMAGE_API_KEY`，再从该会话启动 AstrBot。

通过插件管理安装并配置。插件会使用 AstrBot 的临时目录，不要求固定安装位置。

## 手动安装

在 **实际 AstrBot 数据目录**打开终端，执行：

```sh
git clone https://github.com/Helloworld2345567/astr-bot-sakura.git plugins/sakura_auto_illustration
python -m pip install -r plugins/sakura_auto_illustration/requirements.txt
```

这里的 Python 必须是运行 AstrBot 的 Python；不要覆盖已经存在的同名插件目录。桌面模式、Docker 与源码模式的数据目录可能不同，请通过 AstrBot 部署配置确认；维护目录中名为 `data` 的文件夹不一定是运行数据。

设置密钥环境变量后重启 AstrBot，再在插件配置填写服务参数。更新已通过此方式安装的插件时，先备份本地改动，再在插件目录执行 `git pull --ff-only` 并重载。

## 常见问题

插件内附 `skills/image-generation/`，AstrBot 支持发现插件提供的 Skill。可在技能管理中检查可用性，并按需选择目标人格。若已有同名的用户安装技能，请先核对来源，避免误用旧版私有服务配置；本仓库不会覆盖现有技能或修改其启用状态。

需要独立安装时，复制完整的 `skills/image-generation/` 目录到实际 AstrBot 数据目录下的 `skills/`。脚本是自包含的，只需要 `requests`；不要只复制 `SKILL.md`。独立调用的 API 地址、模型、密钥变量名以 Skill 说明为准，WebUI 插件设置不会自动同步到独立脚本。

- **完全没有配图**：确认目标会话使用 `persona_id` 对应的人格，消息不包含命令或 `[]`，且已配置图片服务密钥。配置名称必须与真实人格名称一致。
- **文字成功、配图失败**：聊天模型和图片服务是两套配置；检查图片服务余额、Key 权限、模型、网络及 JPEG 参数支持情况。不要把 Key 粘贴到日志或 Issue。
- **参考图请求失败**：服务商必须支持 `/images/edits` 的 multipart `image` 字段。每张输入不超过 10 MiB，单条输入最多 4 张，合计上传不超过 22 MiB。可重新发送较小的 PNG/JPEG/WebP 图片。
- **请求按参考图生成，但提示重发**：缓存可能已过期、会话已切换或序号超出当前输入范围。直接引用目标图片或重新发送，再明确说明修改要求。
- **发出两条消息仍分别回复**：默认合并窗口为 2 秒，只合并尚在等待窗口内的消息；可在插件配置增大 `merge_window_seconds`，上限 10 秒。
- **不能使用电脑工具**：管理员白名单默认留空，且工具自身需要权限检查。不要为了解决配图问题开启电脑管理权限。

从已有个人部署迁移时，只安装仓库文件，并在管理面板重新配置。不要复制整份 AstrBot `cmd_config.json`、平台凭据、人格数据库或旧权限名单来分享部署。
