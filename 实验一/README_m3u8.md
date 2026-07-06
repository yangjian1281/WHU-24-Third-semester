# m3u8 流媒体爬取实验

本实验脚本对应 `数据爬取1.pdf` 的拓展练习：

1. 使用 `BeautifulSoup + re` 从网页中结构化匹配 m3u8 地址。
2. 解析 m3u8 文件，下载全部 ts 分片并合并为视频。
3. 遇到 `#EXT-X-KEY:METHOD=AES-128` 的加密 m3u8 时，自动下载密钥并解密分片。

## 安装依赖

```powershell
pip install -r 实验一/requirements.txt
```

如果只下载未加密 m3u8，`requests` 和 `beautifulsoup4` 就够用；如果下载加密 m3u8，需要 `pycryptodome`。

## 运行示例

从 AcFun 页面中自动提取 m3u8 地址并下载：

```powershell
python 实验一/m3u8_crawler.py --page-url "https://www.acfun.cn/v/ac33856487" --output downloads/acfun_demo.mp4
```

如果网页没有直接暴露 m3u8，可以先用浏览器 F12 的 Network 面板筛选 `m3u8`，再把地址交给脚本：

```powershell
python 实验一/m3u8_crawler.py --m3u8-url "https://example.com/path/index.m3u8" --output downloads/direct_demo.mp4
```

加密 m3u8 的命令不需要额外参数，脚本会根据 `#EXT-X-KEY` 自动识别并解密：

```powershell
python 实验一/m3u8_crawler.py --m3u8-url "https://example.com/encrypted/index.m3u8" --output downloads/encrypted_demo.mp4
```

需要本机已安装 `ffmpeg`，脚本会调用它把合并后的 ts 转成 mp4；如果没有 `ffmpeg`，脚本仍会保留合并后的 `.ts` 文件。
