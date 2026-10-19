# 云端部署说明（GitHub Actions）

把扫描器放到 GitHub 的免费定时任务里，**电脑关机、手机锁屏都照跑**，企业微信照常收信号。

- 频率：每 15 分钟一轮（对齐 15M 收盘后约 3 分钟）
- 成本：0 元（公开仓库的 Actions 分钟数不计量）
- 需要：一个 GitHub 账号 + 一个企业微信群机器人 webhook（你已有）

---

## 一、创建仓库

1. 登录 https://github.com （没有账号就先注册，免费）
2. 右上角 `+` → **New repository**
3. 填写：
   - Repository name：`crypto-perp-scan`（随便起）
   - 可见性选 **Public**（重要：私有仓库每月只有 2000 分钟，每 15 分钟一轮会超额；公开仓库不计分钟）
   - 不要勾 "Add a README file"
4. 点 **Create repository**

> 公开仓库只是代码公开，**webhook 不会出现在代码里**，它存在 Secrets 中，别人看不到。本目录的 `config.json` 已经把 webhook 清空，放心上传。

## 二、上传两个文件

在新建的仓库页面，点 **Add file → Upload files**，把本目录（`cloud/`）里的这两个文件拖进去：

- `scanner.py`
- `config.json`

然后点绿色 **Commit changes**。

## 三、创建定时任务

1. 仓库页 → **Add file → Create new file**
2. 文件名框里直接输入（斜杠会自动建目录）：

```
.github/workflows/scan.yml
```

3. 把下面内容完整粘贴进去：

```yaml
name: perp-resonance-scan

on:
  schedule:
    - cron: "4,19,34,49 * * * *"
  workflow_dispatch: {}

permissions:
  contents: read

concurrency:
  group: perp-scan
  cancel-in-progress: false

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - name: Restore push state
        uses: actions/cache@v4
        with:
          path: state
          key: state-${{ github.run_id }}
          restore-keys: |
            state-

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Check secret
        env:
          WECOM_WEBHOOK: ${{ secrets.WECOM_WEBHOOK }}
        run: |
          if [ -z "$WECOM_WEBHOOK" ]; then
            echo "::error::缺少仓库 Secret: WECOM_WEBHOOK"
            exit 1
          fi
          echo "webhook secret OK"

      - name: Scan and push
        env:
          WECOM_WEBHOOK: ${{ secrets.WECOM_WEBHOOK }}
        run: python scanner.py
```

4. 点 **Commit changes**

## 四、填入 webhook（关键一步）

1. 仓库页 → **Settings**（顶部标签）
2. 左侧 **Secrets and variables → Actions**
3. 点 **New repository secret**
4. Name 填：`WECOM_WEBHOOK`
5. Secret 填你的完整地址：

```
https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=88da2c14-c519-4137-8f5d-571f7d22334b
```

6. 点 **Add secret**

## 五、手动试跑一次

1. 仓库页 → **Actions** 标签
2. 左侧点 `perp-resonance-scan` → 右侧 **Run workflow** → 绿色按钮
3. 等 1~4 分钟，点进这次运行，能看到：
   - `webhook secret OK`
   - `样本 590 个合约 / 有效 590 / ... 耗时 xx s`
   - 若有信号：`[push] 发送 N 条信号，企业微信返回: {"errcode": 0, ...}`
   - 若无信号：`[quiet] 本轮无可推送信号…不发送消息`

看到 `样本 590` 就说明跑通了。之后**每 15 分钟自动运行**，不用管。

---

## 六、日常维护

| 想做的事 | 怎么做 |
|---|---|
| 暂停扫描 | Actions → `perp-resonance-scan` → 右侧 `···` → **Disable workflow** |
| 恢复 | 同一个菜单 → **Enable workflow** |
| 改频率 | 编辑 `.github/workflows/scan.yml` 里的 `cron`（UTC 时间，用 https://crontab.guru 换算） |
| 改推送规则/风控参数 | 直接编辑仓库里的 `config.json`，下一轮生效 |
| 清空 12 小时冷却 | 删掉该仓库 Actions 的缓存：Actions 页 → Caches → 删除 `state-` 开头的条目 |
| 看今天推了几条 | 只能在本地跑 `python scanner.py --status`（云端状态存在缓存里，看不到细节） |

## 七、必须知道的四个限制

1. **公开仓库的定时任务连续 60 天没有任何提交会被自动停用**。每月随便改一下 `README.md`（加个空格删掉）提交一次即可。
2. **GitHub 的定时任务不保证准点**，高峰期可能延迟 5~20 分钟，极端情况会漏掉某一轮。这不是程序问题。
3. **云端没有本地 K 线缓存**，每轮都是全量拉取，单轮约 2~4 分钟。所以 15 分钟一轮实际是"跑 3 分钟、等 11 分钟"。
4. **github.com 在国内可能加载慢**，只有创建/修改时需要打开网页；跑起来以后是 GitHub 的服务器在跑，跟你的网络无关。

## 八、关于手机

云端方案下，手机**什么都不用装**：信号直接进企业微信群。想在手机上看运行日志，装 GitHub App 即可，但没必要。

---

## 附：如果不想用 GitHub

备选是腾讯云函数 SCF（国内节点，免费额度足够，定时最小 1 分钟）。差别：

- 优点：国内访问 Gate 更稳，不受 GitHub 60 天限制
- 缺点：需要腾讯云账号实名认证，手动上传 zip、配置定时触发器、环境变量，且 `/tmp` 不持久化，12 小时冷却状态需要额外接 COS 存储

需要这套方案的话告诉我，我按 SCF 的目录结构重新打包。
