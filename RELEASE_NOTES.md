# RAID 阵列健康 v1.10.45

## 本次发布

- **HBA 下 ZFS 池识别**：读取 `zpool list` / `zpool status -P`，支持 RAIDZ/RAIDZ2/RAIDZ3、mirror 等 vdev
- 修复 zpool 输出转义换行、制表符和 `/dev/disk/by-partuuid/*` 成员路径解析问题
- ZFS 池显示在所属 HBA 控制器卡片内，成员盘 RAID 列显示 `ZFS RAIDZ2` 等标识
- 控制器卡片采用底部对齐布局，磁盘数量 / 拓扑按钮固定贴底
- ZFS 控制器卡片显示与首页一致的应用版本徽标
- 同一 RAID 组使用统一颜色，不同 RAID 组使用不同颜色
- 每轮采集增加 `lsblk` / `zpool list` / `zpool status` 命令缓存，避免同一轮重复扫描
- 手机端顶部只保留用户和展开按钮，其余操作进入下拉菜单
- 手机端筛选、RAID 维护检查支持单按钮折叠
- 日志中心统一承载审计日志、健康事件和导出；移除维护面板审计视图和旧 `/audit` 入口
- 审计用户名按 UID 归一化；底部信息改为中文
- 「其他错误」提示关闭后持久隐藏
- 硬件控制台安全码改存 `@appconf/diskhealth`，应用升级/重装不再丢失；不提供在线重置
- 硬件控制台增加实验性风险提示与确认、扩容新盘校验
- 安装图标、控制台 / 日志中心品牌与首页统一

## 安装与升级

```bash
fnos-cli app install-fpk diskhealth-v1.10.45.fpk --volume-id 1 --yes
```

升级前建议：

- 备份重要数据
- 记录当前 RAID / ZFS 状态
- 保留旧版本安装包以便回退
- 使用应用中心升级时不要卸载应用，避免应用数据目录被重置

安全码保存在：

```text
/usr/local/apps/@appconf/diskhealth/console_security.json
```

忘记安全码仅能由管理员 SSH 删除该文件并重启应用；不提供在线重置入口。

## 许可证

GPL-3.0-or-later
