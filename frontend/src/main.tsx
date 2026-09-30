import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "./index.css";

// Flat Design 主题：纯色块、无阴影、扁平边框、几何字体
const flatTheme = {
  token: {
    colorPrimary: "#3B82F6",
    colorSuccess: "#10B981",
    colorWarning: "#F59E0B",
    colorError: "#EF4444",
    colorInfo: "#3B82F6",
    colorTextBase: "#111827",
    colorBorder: "#E5E7EB",
    colorBorderSecondary: "#E5E7EB",
    colorBgLayout: "#F3F4F6",
    borderRadius: 8,
    fontFamily:
      "'Outfit', -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Segoe UI', Roboto, sans-serif",
    // Flat Design：彻底移除阴影，用色块与边框构建层次
    boxShadow: "none",
    boxShadowSecondary: "none",
    boxShadowTertiary: "none",
    wireframe: false,
  },
  components: {
    Layout: {
      siderBg: "#111827",
      triggerBg: "#111827",
      headerBg: "#FFFFFF",
      bodyBg: "#F3F4F6",
    },
    Menu: {
      darkItemBg: "#111827",
      darkSubMenuItemBg: "#111827",
      darkItemColor: "#9CA3AF",
      darkItemHoverColor: "#FFFFFF",
      darkItemSelectedBg: "#3B82F6",
      darkItemSelectedColor: "#FFFFFF",
      itemBorderRadius: 8,
    },
    Card: {
      boxShadowTertiary: "none",
    },
    Button: {
      primaryShadow: "none",
      defaultShadow: "none",
      dangerShadow: "none",
    },
    Table: {
      headerBg: "#F3F4F6",
      borderColor: "#E5E7EB",
    },
  },
};

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={flatTheme} wave={{ disabled: true }}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>
);
