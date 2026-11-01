# MoviePilot 插件

MoviePilot V2 / V3 第三方插件源。

## 安装

在 MoviePilot 的插件市场设置中添加：

```text
https://github.com/lj0014/MoviePilot-Plugins/
```

刷新插件市场，搜索并安装 **迅雷字幕**。

同一个仓库地址供 V2、V3 使用，各版本自动选择对应实现，无需额外添加一条源。

| 宿主 | 插件版本 | 索引与源码 |
| --- | --- | --- |
| MoviePilot V2 | 1.3.0 | `package.v2.json` / `plugins.v2/thundersubtitles/` |
| MoviePilot V3.2.3 及以上 | 2.1.0 | `package.v3.json` / `plugins.v3/thundersubtitles/` |

V2 已声明不向 V3 回退提供旧实现。两版同步维护下载功能，插件直接分发源码，无需下载 Release 压缩包。
`package.json` 保持为空，不提供 MoviePilot V1 实现。一个宿主只加载自己对应的版本。

V3 版使用稳定 SDK、类型化整理事件和宿主一次性任务；配置字段、插件 ID 及历史格式沿用 V2。
新建 V3 配置默认中英双语优先，升级已有配置时保留原偏好。[V3 安装与测试说明](plugins.v3/thundersubtitles/README.md)。

## 迅雷字幕

整理入库后，合并视频 CID 匹配与 HTTPS 名称搜索结果，下载中文字幕并保存到视频旁边。使用与 [Kodi 迅雷字幕插件](https://github.com/lj0014/kodi_thunder_subtitles) 相同的哈希算法和字幕接口，无需 ChineseSubFinder 服务器或 API 密钥。

### 功能

- 监听整理完成事件，后台下载字幕，支持电影和剧集。
- 手动扫描或定时扫描已入库的影片目录。
- 使用入库前原名和入库后文件名补搜；CID 与名称结果按下载地址去重，单个接口失败时保留其他查询结果。
- 保留新入库文件的原始名称，优先选择片源、剪辑版和发布组线索更接近的字幕，再按语言偏好、评分和票数排序。
- 支持简体、繁体或中英双语优先；明确冲突的剪辑版、集数和分碟会被跳过。
- 下载后检查正文是否包含中文，候选无效时尝试其他字幕。
- 支持 SRT、ASS、SSA、SMI、VTT 和 ZIP，字幕可保存为 UTF-8。
- 默认跳过已有外置中文字幕，始终保留原有同名文件。
- 默认读取内置字幕轨道，已有明确标注的完整中文字幕时跳过；可改为仅在已有中英字幕时跳过，或关闭内置检测。
- 读取视频时长，结合迅雷返回的字幕结束时间辅助排序；下载后核对正文时间轴，避免明显超出视频的候选。
- 插件详情页显示最近 50 条处理记录，包括字幕原名、查询来源、原视频名称及版本匹配线索。

### 配置

| 配置 | 说明 |
| --- | --- |
| 启用插件 | 自动处理之后整理入库的视频 |
| 保存后扫描一次 | 保存配置后扫描指定目录，执行后开关自动关闭；可单独使用 |
| 扫描目录 | MoviePilot 能直接访问的绝对路径，每行一个；只处理新入库文件时可留空 |
| 扫描周期 | 五段 cron，例如 `0 */6 * * *` 为每六小时扫描；留空关闭定时扫描 |
| 字幕偏好 | 在版本匹配排序之后选择简体、繁体或中英双语；没有首选时尝试其他中文字幕，不转换简繁 |
| 已有外置中文字幕时跳过 | 识别同名及 `.zh`、`.chs`、`.cht` 等常见中文标签 |
| 内置字幕跳过规则 | 默认已有明确标注的中文字幕时跳过；可选仅中英字幕或不检查。已知强制、路牌、歌曲及特效轨道不作完整字幕 |
| 按时长辅助筛选字幕 | 默认开启；只降低明显异常候选的优先级，不按结尾差值越小越好来排序 |
| ffprobe 路径 | 留空从运行环境自动查找；找不到时可填容器内可执行文件的绝对路径，不填写额外参数 |
| 字幕保存为 UTF-8 | 默认开启；关闭后保留下载文件的原编码 |
| 事件路径前缀／容器内路径前缀 | 可选，仅在整理事件中的路径与实际挂载路径不一致时填写 |

保存示例：

```text
/media/movies/Forrest Gump (1994)/Forrest Gump (1994).mkv
/media/movies/Forrest Gump (1994)/Forrest Gump (1994).zh.srt
```

ASS、SSA 等字幕沿用对应扩展名。插件一次为一个视频保存一份字幕，不覆盖已有文件。

### 内置字幕与时长检查

使用 ffprobe 一次读取字幕轨道标签和容器时长，不扫描全片帧或执行音频对齐，单次读取超时为 8 秒。
未安装 ffprobe、读取失败或超时后继续普通匹配，并在处理记录中说明。插件不会自行安装程序或修改宿主依赖。
内置字幕判断依赖语言和轨道名称；语言不明的轨道不自动跳过。轨道标签可能错误，无法仅凭标签证明字幕完整或同步。
压在画面里的硬字幕不能通过轨道信息检测。中英模式需要轨道名称明确标注中英或同时标注中文和英文。

迅雷的 `duration` 在已核对的 SRT 样本中是最后一句字幕结束时间，不能当作影片总时长。
接口时间只作预排序，不据此直接丢弃字幕；下载后读取 SRT、ASS、SSA、VTT 的正文结束时间再核对。
目前保守处理以下情况：

- 结束时间超出视频结尾超过 `60 秒` 和 `视频时长的 3%` 中的较大值：正文核对后排除。
- 对 10 分钟及以上的视频，字幕结束时间不足视频时长的 65%：降分并尝试其他候选；只有这类候选可用时仍可保存，记录中提示可能为片段或分碟。
- 缺少有效时间、正常片尾无对白，以及结束时间接近的候选：不额外加减分。

保留剪辑版、片源、发布组和语言偏好的匹配依据，最多尝试下载 10 个候选。
SMI 只有开始时间时不把它当作结束时间；时长检测失败不影响原有下载。
这些规则不能识别全片固定提前或延后几秒的问题，也不会自动调整字幕时间轴。

### Docker 路径

视频目录必须以可读写方式挂载到 MoviePilot 容器内。扫描目录填写容器内路径，例如 `/media/movies`，不是 NAS 主机上的路径。

如整理事件使用 `/downloads/library`，而容器实际挂载在 `/media/library`，可配置：

```text
事件路径前缀：/downloads/library
容器内路径前缀：/media/library
```

路径替换只匹配完整目录前缀，不会把 `/downloads/library2` 当成同一目录。

### 限制

- 视频需能读取和随机定位。未挂载云盘、STRM、蓝光目录和 DVD 目录不处理。
- 同时使用 CID 接口和 `https://api-shoulei-ssl.xunlei.com/oracle/subtitle` 名称接口；只发送 CID 和文件名，不上传视频内容或本地目录。迅雷字幕库不一定包含当前视频版本。
- 名称补搜可能包含其他年份、剪辑版、集数或分碟。明确的年份、剪辑版、集数和分碟冲突会被跳过，仍不能保证每份字幕同步。
- CID 查询返回的候选不保证全部同步。版本线索和时长只辅助选择，不执行音频对齐；同为 BluRay 或同一发布组仍可能有片头偏移。
- 扫描已有影片时使用当前文件名。文件已重命名且不含版本信息时，无法补回丢失的线索；记录会显示“无版本匹配线索”。
- 不支持 RAR、7z、图片字幕及 CD 多碟字幕自动合并。ZIP 中明确标注的其他集数、其他分碟会被跳过。
- 下载和解压总大小上限为 20 MiB。
- 未标记语言的同名外置字幕也按已有字幕处理。仅有 `.en.srt` 等英文标签字幕时仍会尝试下载中文。
- 中文字幕识别结合语言标记和正文中文字符检查，无法保证每份字幕的翻译质量及时间轴准确度。
- 任务队列上限 200。队列满时可通过目录扫描补全；查询失败和无匹配结果不会被永久缓存。

## 开发

字幕搜索、匹配和下载功能同时维护 V2、V3，并分别运行回归测试；宿主 SDK 和事件适配按各版本规范处理。

```sh
python -m pip install -r requirements-dev.txt
python tools/check_source.py
python -m unittest discover -s tests -v
```

参考 [MoviePilot V2 开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md) 和 [ChineseSubFinder 插件](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins.v2/chinesesubfinder/__init__.py)。

V3 按[插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)
及[迁移规范](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V3_Plugin_Adaptation.md)维护。
在同级目录准备 MoviePilot V3 源码及其官方依赖环境后运行：

```sh
MOVIEPILOT_PATH=../MoviePilot ../MoviePilot/.venv/bin/python -m pytest tests/v3/thundersubtitles -q
```

V2 测试和 V3 测试在独立进程运行，V3 通过宿主 `app.testing` 隔离配置与数据库，
使用生产命名空间 `app.plugins.thundersubtitles` 加载；测试不连接用户的真实配置和媒体库。
V3 未新增第三方依赖，复用宿主提供的 requests、chardet 和 APScheduler，不安装或覆盖宿主依赖。
媒体检测还使用运行环境中的 ffprobe；缺少它时保留普通下载功能。安装了 ffmpeg 和 ffprobe 的测试环境会额外生成短视频验证实际轨道与时长读取。

已在 MoviePilot V2 验证整理入库事件、自动下载和已有字幕跳过。版本排序有自动测试覆盖，字幕是否同步仍需播放确认。

## 许可

GPL-2.0-or-later。字幕文件的权利归其各自权利人所有。
