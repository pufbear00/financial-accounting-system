import { useEffect, useState } from "react";
import { Table, DatePicker, Select, Input, Tag, Card, Statistic, Row, Col, Button, Modal, List, Badge, Switch, Segmented, message } from "antd";
import { CommentOutlined, CheckCircleOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useSearchParams } from "react-router-dom";
import { listApprovals, listCategories, updateApprovalCategory, updateApprovalCurrency, detectCurrency, listComments, addComment, resolveComment, listAirwallex, updateAirwallexCategory } from "../../api/client";
import type { Approval, Category, ApprovalListResponse, UserInfo } from "../../types";

const STATUS_MAP: Record<number, { text: string; color: string }> = {
  1: { text: "待审批", color: "processing" },
  2: { text: "已通过", color: "success" },
  3: { text: "已拒绝", color: "error" },
  4: { text: "已撤销", color: "default" },
};

export default function ApprovalList({ user }: { user?: UserInfo }) {
  const isAdmin = user?.role === "admin";
  const [searchParams] = useSearchParams();
  const urlCategoryId = searchParams.get("category_id");
  const [source, setSource] = useState<string>("feishu");
  const [data, setData] = useState<ApprovalListResponse>({ items: [], total: 0, page: 1, page_size: 20, sum_amount: 0 });
  const [awxData, setAwxData] = useState<{ items: unknown[]; total: number; page: number; page_size: number; sum_amount: number }>({ items: [], total: 0, page: 1, page_size: 20, sum_amount: 0 });
  const [categories, setCategories] = useState<Category[]>([]);
  const [filters, setFilters] = useState<Record<string, unknown>>({
    page: 1, page_size: 20,
    ...(urlCategoryId ? { category_id: Number(urlCategoryId) } : {}),
  });

  // 反馈相关
  const [commentModal, setCommentModal] = useState<{ approvalId: number; description: string } | null>(null);
  const [comments, setComments] = useState<{ id: number; user_name: string; content: string; resolved: number; created_at: string }[]>([]);
  const [newComment, setNewComment] = useState("");

  const load = async (f: Record<string, unknown>) => {
    try {
      const clean = Object.fromEntries(Object.entries(f).filter(([, v]) => v !== undefined && v !== null && v !== ""));
      if (source === "airwallex") {
        setAwxData(await listAirwallex(clean));
      } else {
        setData(await listApprovals(clean));
      }
    } catch {
      message.error("加载失败");
    }
  };

  useEffect(() => { listCategories().then(setCategories).catch(() => {}); }, []);
  useEffect(() => { setFilters((prev) => ({ ...prev, page: 1 })); }, [source]);
  useEffect(() => { load(filters); }, [filters, source]);

  const updateFilter = (key: string, value: unknown) => {
    setFilters((prev) => ({ ...prev, [key]: value, page: 1 }));
  };

  const openComments = async (approvalId: number, description: string) => {
    setCommentModal({ approvalId, description });
    setNewComment("");
    try { setComments(await listComments(approvalId)); } catch { setComments([]); }
  };

  const submitComment = async () => {
    if (!newComment.trim() || !commentModal) return;
    try {
      await addComment(commentModal.approvalId, newComment.trim());
      setNewComment("");
      setComments(await listComments(commentModal.approvalId));
      message.success("反馈已提交");
    } catch { message.error("提交失败"); }
  };

  const handleResolve = async (commentId: number) => {
    try {
      await resolveComment(commentId);
      if (commentModal) setComments(await listComments(commentModal.approvalId));
    } catch { message.error("操作失败"); }
  };

  // 飞书审批列
  const feishuColumns = [
    { title: "审批类型", dataIndex: "definition_name", width: 100 },
    { title: "事由", dataIndex: "description", ellipsis: true },
    {
      title: "金额", width: 130,
      render: (_: unknown, row: Approval) => {
        const prefix = (row as unknown as { currency?: string }).currency === "USD" ? "$" : "¥";
        return row.amount != null ? `${prefix}${row.amount.toLocaleString()}` : "-";
      },
    },
    {
      title: "币种", width: 80,
      render: (_: unknown, row: Approval) => {
        const cur = (row as unknown as { currency?: string }).currency || "CNY";
        return isAdmin ? (
          <Select size="small" value={cur} style={{ width: 70 }}
            options={[{ label: "CNY", value: "CNY" }, { label: "USD", value: "USD" }]}
            onChange={async (v) => { try { await updateApprovalCurrency(row.id, v); load(filters); } catch { message.error("修改失败"); } }} />
        ) : <Tag>{cur}</Tag>;
      },
    },
    {
      title: "汇率", width: 60,
      render: (_: unknown, row: Approval) => {
        const rate = (row as unknown as { exchange_rate?: number }).exchange_rate;
        const cur = (row as unknown as { currency?: string }).currency;
        return cur === "CNY" || !cur ? "1" : rate ? rate.toFixed(2) : "-";
      },
    },
    {
      title: "折合¥", width: 110,
      render: (_: unknown, row: Approval) => {
        const cny = (row as unknown as { amount_cny?: number }).amount_cny;
        return cny != null ? `¥${cny.toLocaleString()}` : (row.amount != null ? `¥${row.amount.toLocaleString()}` : "-");
      },
      sorter: (a: Approval, b: Approval) => {
        const aCny = (a as unknown as { amount_cny?: number }).amount_cny ?? a.amount ?? 0;
        const bCny = (b as unknown as { amount_cny?: number }).amount_cny ?? b.amount ?? 0;
        return aCny - bCny;
      },
    },
    {
      title: "分类", width: 160,
      render: (_: unknown, row: Approval) =>
        isAdmin ? (
          <Select size="small" value={row.category_id || undefined} placeholder="未分类" style={{ width: 140 }}
            options={categories.map((c) => ({ label: c.name, value: c.id }))}
            onChange={async (v) => { try { await updateApprovalCategory(row.id, v); load(filters); } catch { message.error("修改失败"); } }} />
        ) : (
          <span>{categories.find((c) => c.id === row.category_id)?.name || <Tag>未分类</Tag>}</span>
        ),
    },
    {
      title: "提交日", dataIndex: "submit_date", width: 105,
      render: (v: string | null, row: Approval) => v || row.expense_date || "-",
    },
    { title: "申请人", dataIndex: "applicant_name", width: 80 },
    {
      title: "状态", dataIndex: "status", width: 90,
      render: (v: number) => { const s = STATUS_MAP[v]; return s ? <Tag color={s.color}>{s.text}</Tag> : v; },
    },
    {
      title: "反馈", width: 70,
      render: (_: unknown, row: Approval) => (
        <Button type="text" size="small" icon={<CommentOutlined />}
          onClick={() => openComments(row.id, row.description || "")} />
      ),
    },
  ];

  // Airwallex 列
  const awxColumns = [
    { title: "商户", dataIndex: "merchant_name", ellipsis: true },
    { title: "持卡人", dataIndex: "card_nickname", width: 110 },
    { title: "币种", dataIndex: "currency", width: 60 },
    {
      title: "原币金额", dataIndex: "amount", width: 110,
      render: (v: number | null) => {
        if (v == null) return "-";
        const color = v > 0 ? "#52c41a" : undefined;
        return <span style={{ color }}>{v.toLocaleString()}</span>;
      },
    },
    { title: "汇率", dataIndex: "exchange_rate", width: 70, render: (v: number | null) => v != null ? v.toFixed(2) : "-" },
    {
      title: "折合人民币", dataIndex: "amount_cny", width: 110,
      render: (v: number | null) => {
        if (v == null) return "-";
        const color = v > 0 ? "#52c41a" : undefined;
        return <span style={{ color }}>¥{v.toLocaleString()}</span>;
      },
    },
    {
      title: "分类", width: 160,
      render: (_: unknown, row: { id: number; category_id: number | null }) =>
        isAdmin ? (
          <Select size="small" value={row.category_id || undefined} placeholder="未分类" style={{ width: 140 }}
            options={categories.map((c) => ({ label: c.name, value: c.id }))}
            onChange={async (v) => { try { await updateAirwallexCategory(row.id, v); load(filters); } catch { message.error("修改失败"); } }} />
        ) : (
          <span>{categories.find((c) => c.id === row.category_id)?.name || <Tag>未分类</Tag>}</span>
        ),
    },
    { title: "日期", dataIndex: "transaction_date", width: 110 },
    { title: "商户类别", dataIndex: "merchant_category", width: 100 },
  ];

  const currentData = source === "airwallex" ? awxData : data;

  return (
    <div>
      <Row gutter={16} style={{ marginBottom: 16 }} align="middle">
        <Col>
          <Segmented
            value={source}
            onChange={(v) => setSource(v as string)}
            options={[
              { label: "飞书审批", value: "feishu" },
              { label: "Airwallex", value: "airwallex" },
            ]}
          />
        </Col>
        <Col>
          <DatePicker picker="month" placeholder="筛选月份" onChange={(d) => updateFilter("year_month", d ? d.format("YYYY-MM") : undefined)} allowClear />
        </Col>
        <Col>
          <Select placeholder="费用分类" allowClear style={{ width: 160 }}
            value={filters.category_id as number | undefined}
            onChange={(v) => updateFilter("category_id", v)}
            options={[{ label: "⚠ 未分类", value: 0 }, ...categories.map((c) => ({ label: c.name, value: c.id }))]} />
        </Col>
        {source === "feishu" && (
          <>
            <Col>
              <Select placeholder="审批类型" allowClear style={{ width: 140 }} onChange={(v) => updateFilter("definition_name", v)}
                options={[{ label: "费用报销", value: "费用报销" }, { label: "付款申请", value: "付款申请" }, { label: "海外费用付款申请", value: "海外费用付款申请" }]} />
            </Col>
            <Col>
              <Select placeholder="状态" allowClear style={{ width: 120 }} onChange={(v) => updateFilter("status", v)}
                options={Object.entries(STATUS_MAP).map(([k, v]) => ({ label: v.text, value: Number(k) }))} />
            </Col>
            <Col>
              <Input.Search placeholder="搜索申请人" allowClear style={{ width: 160 }}
                onSearch={(v) => updateFilter("applicant_name", v || undefined)} />
            </Col>
            {isAdmin && (
              <Col>
                <Switch checkedChildren="有反馈" unCheckedChildren="全部"
                  onChange={(v) => updateFilter("has_feedback", v || undefined)} />
              </Col>
            )}
          </>
        )}
        {source === "airwallex" && (
          <>
            <Col>
              <Input.Search placeholder="搜索商户" allowClear style={{ width: 160 }}
                onSearch={(v) => updateFilter("merchant_name", v || undefined)} />
            </Col>
            <Col>
              <Input.Search placeholder="搜索持卡人" allowClear style={{ width: 140 }}
                onSearch={(v) => updateFilter("card_nickname", v || undefined)} />
            </Col>
          </>
        )}
        <Col flex="auto" style={{ textAlign: "right", display: "flex", gap: 8, justifyContent: "flex-end" }}>
          {source === "airwallex" ? (
            <>
              <Card size="small" style={{ display: "inline-block" }}>
                <Statistic title="原币合计" value={Math.abs(awxData.sum_amount)} precision={2}
                  prefix="$" valueStyle={{ fontSize: 14 }} />
              </Card>
              <Card size="small" style={{ display: "inline-block" }}>
                <Statistic title="折合人民币" value={Math.abs((awxData as { sum_amount_cny?: number }).sum_amount_cny || 0)} precision={2}
                  prefix="¥" valueStyle={{ fontSize: 16, color: "#f59e0b" }} />
              </Card>
            </>
          ) : (
            <Card size="small" style={{ display: "inline-block" }}>
              <Statistic title="折合人民币合计" value={data.sum_amount} precision={2} prefix="¥" valueStyle={{ fontSize: 16, color: "#f59e0b" }} />
            </Card>
          )}
        </Col>
      </Row>

      <Table
        dataSource={currentData.items as never[]}
        columns={(source === "airwallex" ? awxColumns : feishuColumns) as never[]}
        rowKey="id"
        size="middle"
        pagination={{
          current: currentData.page, pageSize: currentData.page_size, total: currentData.total,
          showTotal: (t) => `共 ${t} 条`, showSizeChanger: true,
          onChange: (p, ps) => setFilters((prev) => ({ ...prev, page: p, page_size: ps })),
        }}
      />

      <Modal title="反馈" open={!!commentModal} onCancel={() => setCommentModal(null)} footer={null} width={500}>
        {commentModal && (
          <div>
            <div style={{ background: "#fafafa", padding: 12, borderRadius: 6, marginBottom: 16, fontSize: 13, color: "#666" }}>
              {commentModal.description || "（无事由）"}
            </div>
            <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
              <Input.TextArea rows={2} placeholder="输入反馈内容" value={newComment}
                onChange={(e) => setNewComment(e.target.value)}
                onPressEnter={(e) => { if (!e.shiftKey) { e.preventDefault(); submitComment(); } }} />
              <Button type="primary" onClick={submitComment} disabled={!newComment.trim()}>提交</Button>
            </div>
            <List size="small" dataSource={comments} locale={{ emptyText: "暂无反馈" }}
              renderItem={(item) => (
                <List.Item actions={isAdmin && !item.resolved ? [
                  <Button key="resolve" type="link" size="small" icon={<CheckCircleOutlined />}
                    onClick={() => handleResolve(item.id)}>已处理</Button>
                ] : undefined}>
                  <List.Item.Meta
                    title={<span>{item.user_name}<span style={{ color: "#999", fontSize: 12, marginLeft: 8 }}>
                      {dayjs(item.created_at).format("MM-DD HH:mm")}</span>
                      {item.resolved ? <Badge status="success" text="已处理" style={{ marginLeft: 8, fontSize: 12 }} /> : null}
                    </span>}
                    description={item.content} />
                </List.Item>
              )} />
          </div>
        )}
      </Modal>
    </div>
  );
}
