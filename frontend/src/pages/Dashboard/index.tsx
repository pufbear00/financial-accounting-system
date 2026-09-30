import { useEffect, useState } from "react";
import { Button, Card, Col, Row, Statistic, Table, Tag, DatePicker, message } from "antd";
import { ArrowUpOutlined, WarningOutlined, ExclamationCircleOutlined } from "@ant-design/icons";
import { Bar, Line } from "@ant-design/charts";
import dayjs from "dayjs";
import { getMonthlySummary, getExpenseTrend, getBudgetAlerts, getUncategorized } from "../../api/client";
import { useNavigate } from "react-router-dom";
import type { MonthlySummary, TrendPoint, BudgetAlert } from "../../types";

export default function Dashboard() {
  const [month, setMonth] = useState(dayjs().format("YYYY-MM"));
  const [summary, setSummary] = useState<MonthlySummary | null>(null);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [alerts, setAlerts] = useState<BudgetAlert[]>([]);
  const [uncategorized, setUncategorized] = useState<{ total: number; items: { id: number; source: string; type: string; who: string; amount: number; currency: string; amount_cny: number; date: string; desc: string }[] }>({ total: 0, items: [] });
  const navigate = useNavigate();

  const load = async (ym: string) => {
    const results = await Promise.allSettled([
      getMonthlySummary(ym),
      getExpenseTrend(6),
      getBudgetAlerts(ym),
      getUncategorized(ym),
    ] as const);

    const [summaryResult, trendResult, alertsResult, uncategorizedResult] = results;
    if (summaryResult.status === "fulfilled") setSummary(summaryResult.value);
    if (trendResult.status === "fulfilled") setTrend(trendResult.value);
    if (alertsResult.status === "fulfilled") setAlerts(alertsResult.value);
    if (uncategorizedResult.status === "fulfilled") setUncategorized(uncategorizedResult.value);

    const failed = [
      summaryResult.status === "rejected" && "费用汇总",
      trendResult.status === "rejected" && "费用趋势",
      alertsResult.status === "rejected" && "预算预警",
      uncategorizedResult.status === "rejected" && "待分类费用",
    ].filter(Boolean);
    if (failed.length > 0) {
      message.error(`${failed.join("、")} 加载失败`);
    }
  };

  useEffect(() => { load(month); }, [month]);

  const barData = (summary?.by_category || []).flatMap((c) => [
    { category: c.category_name, type: "实际支出", value: c.amount, category_id: c.category_id },
    ...(c.budget ? [{ category: c.category_name, type: "预算", value: c.budget, category_id: c.category_id }] : []),
  ]);

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <DatePicker
          picker="month"
          value={dayjs(month)}
          onChange={(d) => d && setMonth(d.format("YYYY-MM"))}
          allowClear={false}
        />
      </div>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={4}>
          <Card>
            <Statistic
              title="本月总支出（飞书审批 + Airwallex已结算）"
              value={summary?.total_amount || 0}
              precision={2}
              prefix="¥"
              valueStyle={{ color: "#f59e0b" }}
            />
          </Card>
        </Col>
        <Col span={5}>
          <Card>
            <Statistic title="飞书审批" value={summary?.by_source?.feishu_amount || 0}
              precision={2} prefix="¥" valueStyle={{ fontSize: 18 }}
              suffix={<span style={{ fontSize: 12, color: "#999" }}>{summary?.by_source?.feishu_count || 0}笔</span>} />
          </Card>
        </Col>
        <Col span={5}>
          <Card>
            <Statistic title="Airwallex" value={summary?.by_source?.airwallex_amount || 0}
              precision={2} prefix="¥" valueStyle={{ fontSize: 18 }}
              suffix={<span style={{ fontSize: 12, color: "#999" }}>{summary?.by_source?.airwallex_count || 0}笔</span>} />
          </Card>
        </Col>
        <Col span={5}>
          <Card>
            <Statistic title="飞书已批准 / 待审批" value={summary?.approved_count || 0}
              suffix={<span style={{ fontSize: 14, color: "#1890ff" }}> / {summary?.pending_count || 0}</span>} />
          </Card>
        </Col>
        <Col span={5}>
          <Card>
            <Statistic
              title="预算使用率"
              value={summary?.budget_usage_pct || 0}
              suffix="%"
              prefix={<ArrowUpOutlined />}
              valueStyle={{ color: (summary?.budget_usage_pct || 0) > 80 ? "#cf1322" : "#3f8600" }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={14}>
          <Card title="分类费用 vs 预算">
            {barData.length > 0 ? (
              <Bar
                data={barData}
                xField="category"
                yField="value"
                colorField="type"
                group
                height={Math.max(300, (summary?.by_category?.length || 4) * 70)}
                scale={{ color: { range: ["#f59e0b", "#91caff"] } }}
                label={{
                  text: (datum: { value?: number }) => datum.value ? `¥${datum.value.toLocaleString()}` : "",
                  position: "outside",
                  style: { fontSize: 12, textAlign: "left" },
                }}
                legend={{ color: { position: "top" as const } }}
                axis={{ x: { labelFontSize: 13 }, y: false }}
                style={{ cursor: "pointer" }}
                onReady={(plot: { chart?: { on?: (event: string, cb: (e: { data?: { data?: { type?: string; category_id?: number } } }) => void) => void } }) => {
                  plot.chart?.on?.("element:click", (e: { data?: { data?: { type?: string; category_id?: number } } }) => {
                    const d = e.data?.data;
                    if (d?.type === "实际支出" && d?.category_id) {
                      navigate(`/approvals?category_id=${d.category_id}`);
                    }
                  });
                }}
              />
            ) : (
              <div style={{ height: 300, display: "flex", alignItems: "center", justifyContent: "center", color: "#999" }}>暂无数据</div>
            )}
          </Card>
        </Col>
        <Col span={10}>
          <Card title="近6月费用趋势">
            {trend.length > 0 ? (
              <Line
                data={trend}
                xField="year_month"
                yField="total"
                height={300}
                color="#f59e0b"
                point={{ size: 4 }}
              />
            ) : (
              <div style={{ height: 300, display: "flex", alignItems: "center", justifyContent: "center", color: "#999" }}>暂无数据</div>
            )}
          </Card>
        </Col>
      </Row>

      {alerts.length > 0 && (
        <Card title={<><WarningOutlined style={{ color: "#cf1322" }} /> 预算预警</>}>
          <Table
            dataSource={alerts}
            rowKey="category_id"
            pagination={false}
            size="small"
            columns={[
              { title: "分类", dataIndex: "category_name" },
              { title: "预算", dataIndex: "budget", render: (v: number) => `¥${v.toLocaleString()}` },
              { title: "已支出", dataIndex: "spent", render: (v: number) => `¥${v.toLocaleString()}` },
              {
                title: "使用率",
                dataIndex: "usage_pct",
                render: (v: number) => (
                  <Tag color={v >= 100 ? "red" : "orange"}>{v}%</Tag>
                ),
              },
            ]}
          />
        </Card>
      )}

      {uncategorized.total > 0 && (
        <Card title={<><ExclamationCircleOutlined style={{ color: "#faad14" }} /> 待分类费用 ({uncategorized.total}条)</>} style={{ marginTop: 16 }}>
          <Table
            dataSource={uncategorized.items}
            rowKey={(r) => `${r.source}-${r.id}`}
            pagination={false}
            size="small"
            columns={[
              { title: "来源", dataIndex: "source", width: 90, render: (v: string) => <Tag color={v === "airwallex" ? "blue" : "green"}>{v === "airwallex" ? "Airwallex" : "飞书审批"}</Tag> },
              { title: "类型", dataIndex: "type", width: 120 },
              { title: "申请人/持卡人", dataIndex: "who", width: 100 },
              { title: "金额", width: 100, render: (_: unknown, r: Record<string, unknown>) => `${r.currency === "USD" ? "$" : "¥"}${((r.amount as number) || 0).toLocaleString()}` },
              { title: "折合¥", dataIndex: "amount_cny", width: 100, render: (v: number) => v ? `¥${v.toLocaleString()}` : "-" },
              { title: "日期", dataIndex: "date", width: 100 },
              { title: "描述", dataIndex: "desc", ellipsis: true },
              { title: "操作", width: 80, render: () => (
                <Button type="link" size="small" onClick={() => navigate("/approvals")}>去分类</Button>
              )},
            ]}
          />
        </Card>
      )}
    </div>
  );
}
