# MoviePilot 插件

MoviePilot V2 第三方插件源。

## 安装

在 MoviePilot 的插件市场设置中添加：

```text
https://github.com/lj0014/MoviePilot-Plugins/
```

刷新插件市场，搜索并安装 **迅雷字幕**。

插件通过 `package.v2.json` 和 `plugins.v2/` 目录分发源码，无需下载 Release 压缩包。`package.json` 为空，未提供 MoviePilot V1 或 V3 插件。

## 迅雷字幕

整理入库后，根据视频文件的 CID 匹配迅雷字幕，下载中文字幕并保存到视频旁边。使用与 [Kodi 迅雷字幕插件](https://github.com/lj0014/kodi_thunder_subtitles) 相同的哈希算法和字幕接口，无需 ChineseSubFinder 服务器或 API 密钥。

### 功能

- 监听整理完成事件，后台下载字幕，支持电影和剧集。
- 手动扫描或定时扫描已入库的影片目录。
- 简体、繁体或中英双语优先，按语言偏好、评分和票数排序。
- 下载后检查正文是否包含中文，候选无效时尝试其他字幕。
- 支持 SRT、ASS、SSA、SMI、VTT 和 ZIP，字幕可保存为 UTF-8。
- 默认跳过已有外置中文字幕，始终保留原有同名文件。
- 插件详情页显示最近 50 条处理记录。

### 配置

| 配置 | 说明 |
| --- | --- |
| 启用插件 | 自动处理之后整理入库的视频 |
| 保存后扫描一次 | 保存配置后扫描指定目录，执行后开关自动关闭；可单独使用 |
| 扫描目录 | MoviePilot 能直接访问的绝对路径，每行一个；只处理新入库文件时可留空 |
| 扫描周期 | 五段 cron，例如 `0 */6 * * *` 为每六小时扫描；留空关闭定时扫描 |
| 字幕偏好 | 简体、繁体或中英双语优先；没有首选时尝试其他中文字幕，不转换简繁 |
| 已有外置中文字幕时跳过 | 识别同名及 `.zh`、`.chs`、`.cht` 等常见中文标签；不检查视频内嵌字幕 |
| 字幕保存为 UTF-8 | 默认开启；关闭后保留下载文件的原编码 |
| 事件路径前缀／容器内路径前缀 | 可选，仅在整理事件中的路径与实际挂载路径不一致时填写 |

保存示例：

```text
/media/movies/Forrest Gump (1994)/Forrest Gump (1994).mkv
/media/movies/Forrest Gump (1994)/Forrest Gump (1994).zh.srt
```

ASS、SSA 等字幕沿用对应扩展名。插件一次为一个视频保存一份字幕，不覆盖已有文件。

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
- 通过文件哈希匹配，不支持片名搜索；迅雷字幕库不一定包含当前视频版本。
- 不支持 RAR、7z、图片字幕及 CD 多碟字幕自动合并。ZIP 中明确标注的其他集数、其他分碟会被跳过。
- 下载和解压总大小上限为 20 MiB。
- 未标记语言的同名外置字幕也按已有字幕处理。仅有 `.en.srt` 等英文标签字幕时仍会尝试下载中文。
- 中文字幕识别结合语言标记和正文中文字符检查，无法保证每份字幕的翻译质量及时间轴准确度。
- 任务队列上限 200。队列满时可通过目录扫描补全；查询失败和无匹配结果不会被永久缓存。

## 开发

```sh
python -m pip install -r requirements-dev.txt
python tools/check_source.py
python -m unittest discover -s tests -v
```

参考 [MoviePilot V2 开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md) 和 [ChineseSubFinder 插件](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins.v2/chinesesubfinder/__init__.py)。

插件按 MoviePilot V2 的事件结构和安装目录开发，自动测试覆盖事件处理、下载、编码和文件保存；尚未在实际 MoviePilot 部署中测试。Kodi 插件的实测结果不代表 MoviePilot 插件已经通过实测。

## 许可

GPL-2.0-or-later。字幕文件的权利归其各自权利人所有。
