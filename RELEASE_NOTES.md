# RAID 阵列健康 v1.10.46

## 本次变更

- **飞牛桌面访问默认仅管理员**：`app/ui/config` 的 `allUsers` 设为 `false`，设备内普通用户不再看到应用入口
- Release 附件仅保留安装包与校验文件，不再上传截图图片
- 沿用 v1.10.45 的 ZFS 池识别、控制器卡片底部对齐、采集缓存和手机端适配

## 安装与升级

```bash
fnos-cli app install-fpk diskhealth-v1.10.46.fpk --volume-id 1 --yes
```

- 升级前备份重要数据并保留旧安装包
- 优先应用中心覆盖升级，不要先卸载应用
- 应用数据与安全码配置目录会保留

## 许可证

GPL-3.0-or-later
