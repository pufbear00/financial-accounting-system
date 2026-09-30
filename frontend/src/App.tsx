import { useState, useEffect } from "react";
import { Routes, Route } from "react-router-dom";
import { Spin } from "antd";
import AppLayout from "./components/AppLayout";
import Dashboard from "./pages/Dashboard";
import ApprovalList from "./pages/ApprovalList";
import BudgetManage from "./pages/BudgetManage";
import Categories from "./pages/Categories";
import SyncSettings from "./pages/SyncSettings";
import UserManage from "./pages/UserManage";
import Login from "./pages/Login";
import VoucherBinding from "./pages/VoucherBinding";
import { setToken, getMe } from "./api/client";
import type { UserInfo } from "./types";

export default function App() {
  const [user, setUser] = useState<UserInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem("expense-empty-demo-token");
    if (token) {
      getMe()
        .then(setUser)
        .catch(() => setToken(null))
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  const handleLogin = (token: string) => {
    setToken(token);
    getMe().then(setUser);
  };

  const handleLogout = () => {
    setToken(null);
    setUser(null);
  };

  if (loading) {
    return <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh" }}><Spin size="large" /></div>;
  }

  if (!user) {
    return <Login onLogin={handleLogin} />;
  }

  return (
    <Routes>
      <Route element={<AppLayout user={user} onLogout={handleLogout} />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/approvals" element={<ApprovalList user={user} />} />
        <Route path="/budgets" element={<BudgetManage />} />
        {user.role === "admin" && <Route path="/categories" element={<Categories />} />}
        {user.role === "admin" && <Route path="/settings" element={<SyncSettings />} />}
        {user.role === "admin" && <Route path="/users" element={<UserManage />} />}
        {user.role === "admin" && <Route path="/vouchers" element={<VoucherBinding />} />}
        <Route path="*" element={<Dashboard />} />
      </Route>
    </Routes>
  );
}
