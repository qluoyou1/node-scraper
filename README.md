# 免费节点抓取（GitHub Actions 每日定时版）

每天自动抓取公开免费节点，重命名为 `XX+服务商` 格式，按国家/洲分组，
生成 Clash/Mihomo 可直接导入的配置。

## 部署到 GitHub

1. 新建一个仓库（public 或 private 都行），把本目录所有文件 push 上去
   （含 `.github/workflows/daily-scrape.yml`）
2. 到仓库的 **Actions** 页面，点 `Daily node scrape` → **Run workflow** 手动跑一次
3. 跑完后 `output/clash.yaml` 会自动更新并 commit

## 订阅链接（永久有效）

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/clash.yaml
```

把上面链接填进 FlClash / Bettbox 的订阅/远程配置里，客户端每天会自动拉取最新版。
也可以用 jsdelivr 加速：

```
https://cdn.jsdelivr.net/gh/<你的用户名>/<仓库名>@main/output/clash.yaml
```

## 本地运行

```bash
pip install -r requirements.txt
python3 scraper.py                  # 抓取 -> output/raw/
python3 geoip_rename.py --limit 400 # 重命名+分组 -> output/
```

## 自定义

- `sources.yaml`：增删节点站、订阅链接、TG 频道
- `geoip_rename.py` 里的 `SPECIAL_QUOTAS`：调整各国节点配额；`--limit` 调总数
- `.github/workflows/daily-scrape.yml`：改 cron 定时

## 注意

- 仓库 60 天无任何活动时，GitHub 会自动停掉定时任务，手动 Run 一次即可恢复
- Actions 免费额度：public 仓库无限制，private 仓库每月 2000 分钟（这个任务每次约 2-3 分钟，绰绰有余）
