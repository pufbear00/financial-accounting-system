import { useEffect, useState } from "react";
import { Table, DatePicker, Button, InputNumber, Modal, Select, Progress, message, Space } from "antd";
import { PlusOutlined, CopyOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { listBudgets, createBudget, updateBudget, deleteBudget, copyBudgetFromPrevious, listCategories } from "../../api/client";
import type { BudgetWithSpent, Category } from "../../types";

export default function BudgetManage() {
  const [month, setMonth] = useState(dayjs().format("YYYY-MM"));
  const [budgets, setBudgets] = useState<BudgetWithSpent[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [newCategoryId, setNewCategoryId] = useState<number | null>(null);
  const [newAmount, setNewAmount] = useState<number>(0);

  const load = async () => {
    try {
      const data = await listBudgets(month);
      setBudgets(data);
    } catch {
      message.error("加载预算失败");
    }
  };

  useEffect(() => { listCategories().then(setCategories).catch(() => {}); }, []);
  useEffect(() => { load(); }, [month]);

  const handleAdd = async () => {
    if (!newCategoryId || !newAmount) return;
    try {
      await createBudget({ year_month: month, category_id: newCategoryId, amount: newAmount });
      setModalOpen(false);
      setNewCategoryId(null);
      setNewAmount(0);
      load();
      message.success("预算已添加");
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "添加失败");
    }
  };

  const handleCopy = async () => {
    try {
      const res = await copyBudgetFromPrevious(month);
      message.success(`已复制 ${res.created} 条，跳过 ${res.skipped} 条`);
      load();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "复制失败");
    }
  };

  const columns = [
    { title: "费用分类", dataIndex: "category_name", width: 160 },
    {
      title: "预算金额",
      dataIndex: "amount",
      width: 160,
      render: (v: number, row: BudgetWithSpent) => (
        <InputNumber
          value={v}
          min={0}
          step={1000}
          formatter={(val) => `¥ ${val}`.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}
          parser={(val) => Number((val || "").replace(/[¥,\s]/g, ""))}
          onBlur={(e) => {
            const newVal = Number((e.target.value || "").replace(/[¥,\s]/g, ""));
            if (newVal !== v && newVal > 0) {
              updateBudget(row.id, { amount: newVal }).then(load);
            }
          }}
          style={{ width: 140 }}
        />
      ),
    },
    { title: "已支出", dataIndex: "spent", width: 120, render: (v: number) => `¥${v.toLocaleString()}` },
    {
      title: "使用率",
      width: 200,
      render: (_: unknown, row: BudgetWithSpent) => (
        <Progress
          percent={row.usage_pct}
          status={row.usage_pct >= 100 ? "exception" : row.usage_pct >= 80 ? "active" : "normal"}
          strokeColor={row.usage_pct >= 100 ? "#cf1322" : row.usage_pct >= 80 ? "#faad14" : "#52c41a"}
          format={(pct) => `${pct}%`}
        />
      ),
    },
    {
      title: "操作",
      width: 80,
      render: (_: unknown, row: BudgetWithSpent) => (
        <Button type="link" danger size="small" onClick={() => { deleteBudget(row.id).then(load); }}>
          删除
        </Button>
      ),
    },
  ];

  const usedCategoryIds = new Set(budgets.map((b) => b.category_id));
  const availableCategories = categories.filter((c) => !usedCategoryIds.has(c.id));

  return (
    <div>
      <div style={{ marginBottom: 16, display: "flex", gap: 12, alignItems: "center" }}>
        <DatePicker
          picker="month"
          value={dayjs(month)}
          onChange={(d) => d && setMonth(d.format("YYYY-MM"))}
          allowClear={false}
        />
        <Space>
          <Button icon={<PlusOutlined />} type="primary" onClick={() => setModalOpen(true)} disabled={availableCategories.length === 0}>
            添加预算
          </Button>
          <Button icon={<CopyOutlined />} onClick={handleCopy}>
            从上月复制
          </Button>
        </Space>
      </div>

      <Table dataSource={budgets} columns={columns} rowKey="id" pagination={false} size="middle" />

      <Modal title="添加预算" open={modalOpen} onOk={handleAdd} onCancel={() => setModalOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 16 }}>
          <Select
            placeholder="选择费用分类"
            value={newCategoryId}
            onChange={setNewCategoryId}
            options={availableCategories.map((c) => ({ label: c.name, value: c.id }))}
          />
          <InputNumber
            placeholder="预算金额"
            value={newAmount}
            onChange={(v) => setNewAmount(v || 0)}
            min={0}
            step={1000}
            style={{ width: "100%" }}
            formatter={(val) => `¥ ${val}`.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}
            parser={(val) => Number((val || "").replace(/[¥,\s]/g, ""))}
          />
        </div>
      </Modal>
    </div>
  );
}
