# 🎵 osu!谱面下载 (astrbot_plugin_osu_dl)

<p align="center">
  <img src="logo.png" alt="logo" />
</p>

**astrbot_plugin_osu_dl** 是一个为 [AstrBot](https://github.com/Soulter/AstrBot) 开发的 osu! 谱面解析与下载插件。它可以自动识别聊天中的 osu! 谱面链接，支持命令搜索，并自动将谱面（`.osz`）发送到群聊或私聊中。

## ✨ 功能特点

- 🔗 **链接自动识别**：自动捕捉聊天中的 `osu.ppy.sh/beatmapsets/xxx` 链接并触发下载。
- 🔍 **便捷搜索指令**：支持通过 `/osu <关键词或ID>` 搜索并下载对应谱面。
- 🚀 **智能测速与多节点回退**：自动对多个镜像站（Sayobot, Catboy, osu.direct）进行并发测速，选择最快节点下载。
- 👑 **官网优先支持**：配置 `osu_session` 后可优先使用官方服务器高速下载。
- 📦 **批量打包**：支持同时传入多个谱面 ID 进行批量下载，并自动打包为 ZIP 压缩包发送。
- 🤖 **LLM 工具接入**：为大语言模型提供 `search_osu_beatmap` 和 `download_osu_beatmaps` 工具，允许 AI 自动为你搜歌和下歌。
- 🧹 **任务管理**：内置缓存机制，支持通过 `/osuclear` 随时一键清理卡死的后台下载任务。

## 📥 安装

1. 在 AstrBot 的插件管理器中通过仓库地址安装，或直接将本仓库克隆到 AstrBot 的 `data/plugins/` 目录下：
   ```bash
   git clone https://github.com/timetetng/astrbot_plugin_osu_dl.git
   ```
2. 重启 AstrBot 即可加载插件。

## ⚙️ 配置说明

在 AstrBot 的插件配置面板中，您可以对以下项进行配置（对应 `_conf_schema.json`）：

| 配置项 | 类型 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- |
| `use_official_first` | `bool` | `true` | 是否优先从 osu! 官网下载（如果下载失败会自动回退到镜像站）。 |
| `osu_session` | `string` | `""` | osu! 官网的会话 Cookie。在官网登录后，按 F12 抓包获取（存在时效性）。 |
| `proxy` | `string` | `""` | 访问官网使用的代理地址（例如 `http://127.0.0.1:7890`）。如果服务器在海外或使用旁路由透明代理，可留空。 |

## 💻 指令用法

- `/osu <关键词或ID>`
  **示例**: `/osu 5526026` 或 `/osu galaxy`
  **描述**: 搜索 osu! 谱面。如果是纯数字则直接解析下载；如果是关键词，会返回最多8个搜索结果供你选择，60秒内回复序号即可确认下载。

- `/osuclear`
  **描述**: 强制清理所有卡死的后台 osu! 下载任务与等待队列。

- **隐式触发**：
  在聊天中直接发送包含 `osu.ppy.sh/beatmapsets/<id>` 的链接，机器人会自动识别并开始下载。

## ⚠️ 注意事项

- **文件发送依赖**：本插件使用 `file://` 协议向平台端投递文件。请确保您的平台端（如 Napcat）与 AstrBot 运行在同一宿主机，或者已正确配置了 Docker 目录映射和读写权限，否则可能会出现文件下发失败的问题。
- **缓存清理**：插件会自动在 `/AstrBot/data/osu_cache` 生成缓存文件，缓存有效期为 24 小时，过期后会自动清理，无需担心硬盘堆积。

## 📄 开源协议
本项目采用 [MIT License](LICENSE) 开源协议。

