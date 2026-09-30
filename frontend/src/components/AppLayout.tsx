import { Layout, Menu, Button } from "antd";
import {
  DashboardOutlined,
  TableOutlined,
  FundOutlined,
  TagsOutlined,
  SettingOutlined,
  TeamOutlined,
  LogoutOutlined,
  FileDoneOutlined,
} from "@ant-design/icons";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import type { UserInfo } from "../types";

const { Sider, Content, Header } = Layout;

export default function AppLayout({ user, onLogout }: { user: UserInfo; onLogout: () => void }) {
  const navigate = useNavigate();
  const location = useLocation();

  const menuItems = [
    { key: "/", icon: <DashboardOutlined />, label: "费用总览" },
    { key: "/approvals", icon: <TableOutlined />, label: "审批明细" },
    { key: "/budgets", icon: <FundOutlined />, label: "预算管理" },
    ...(user.role === "admin"
      ? [
          { key: "/categories", icon: <TagsOutlined />, label: "费用分类" },
          { key: "/settings", icon: <SettingOutlined />, label: "同步设置" },
          { key: "/users", icon: <TeamOutlined />, label: "用户管理" },
          { key: "/vouchers", icon: <FileDoneOutlined />, label: "凭证装订" },
        ]
      : []),
  ];

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={200} theme="dark">
        <div
          style={{
            height: 48,
            margin: 16,
            color: "#f59e0b",
            fontSize: 18,
            fontWeight: 700,
            textAlign: "center",
            lineHeight: "48px",
          }}
        >
          ¥ 费用看板
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: "#fff",
            padding: "0 24px",
            fontSize: 18,
            fontWeight: 500,
            borderBottom: "1px solid #f0f0f0",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span>费用审批看板 · 本地空数据版（外部同步已停用）</span>
          <span style={{ fontSize: 14, color: "#666" }}>
            {user.display_name}（{user.role === "admin" ? "管理员" : "查看者"}）
            <Button type="link" icon={<LogoutOutlined />} onClick={onLogout} style={{ marginLeft: 8 }}>
              退出
            </Button>
          </span>
        </Header>
        <Content style={{ margin: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
