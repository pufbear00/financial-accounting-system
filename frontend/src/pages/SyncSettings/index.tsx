import { Alert, Card, Descriptions } from "antd";

export default function SyncSettings() {
  return (
    <Card title="本地空数据版">
      <Alert type="info" showIcon title="全部外部同步已停用"
        description="此独立副本不会拉取审批、支付平台流水、飞书附件或通讯录，也不会发送评论和日报。不要填入原系统账号或密钥。" />
      <Descriptions column={1} style={{ marginTop: 20 }}>
        <Descriptions.Item label="数据">独立本地数据库；首次启动仅生成空表和通用分类</Descriptions.Item>
        <Descriptions.Item label="管理员">首次运行时由使用者单独创建，无内置密码</Descriptions.Item>
        <Descriptions.Item label="展示资料">仅使用自行编写的虚构样例，不放真实发票、简历或合同</Descriptions.Item>
      </Descriptions>
    </Card>
  );
}
