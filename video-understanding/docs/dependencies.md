# 依赖与许可

本项目代码使用 MIT，许可文本位于 LICENSE。独立 ZIP 不携带第三方源码或二进制，安装时解析 requirements.txt。

| 依赖 | 用途 | 上游许可 |
| --- | --- | --- |
| aiohttp >=3.12,<4 | 异步媒体与模型 HTTP | Apache-2.0，依赖自身还各有许可 |
| yt-dlp 2026.8.19 | B站/抖音元数据提取，可替换 worker | 主项目 Unlicense，打包产物/依赖另见上游说明 |
| AstrBot 4.28.2 | 宿主适配与开发测试，仅适配层 | AGPL-3.0，按上游许可使用 |
| FFmpeg / ffprobe | 外部探测、音画合并、转换 | 构建选项决定 LGPL/GPL；本插件未分发 |

核心最低依赖仅 aiohttp；标准库承担本地结构校验和任务存储。yt-dlp 为 links 可选依赖，AstrBot 不进入核心依赖。分发宿主、FFmpeg 或依赖二进制时应重新核查其实际构建、LICENSE/NOTICE 和相应源代码义务。本插件 ZIP 不代替这些依赖的分发许可。

解析器依赖 yt-dlp 固定版本的 RequestDirector 内部扩展点。升级后需要重新执行网络边界测试，不能静默退回默认网络处理器。不存在第二份生图客户端。
