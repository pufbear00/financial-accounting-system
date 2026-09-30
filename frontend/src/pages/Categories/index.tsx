import { useEffect, useState } from "react";
import { Table, Button, Modal, Input, message, Popconfirm } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { listCategories, createCategory, updateCategory, deleteCategory } from "../../api/client";
import type { Category } from "../../types";

export default function Categories() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");

  const load = () => listCategories().then(setCategories).catch(() => message.error("加载失败"));
  useEffect(() => { load(); }, []);

  const handleSave = async () => {
    if (!name.trim()) return;
    try {
      if (editId) {
        await updateCategory(editId, { name: name.trim(), description: desc });
      } else {
        await createCategory({ name: name.trim(), description: desc });
      }
      setModalOpen(false);
      setEditId(null);
      setName("");
      setDesc("");
      load();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "保存失败");
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteCategory(id);
      load();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "删除失败");
    }
  };

  const columns = [
    { title: "ID", dataIndex: "id", width: 60 },
    { title: "名称", dataIndex: "name" },
    { title: "描述", dataIndex: "description" },
    { title: "排序", dataIndex: "sort_order", width: 80 },
    {
      title: "操作",
      width: 140,
      render: (_: unknown, row: Category) => (
        <>
          <Button
            type="link"
            size="small"
            onClick={() => { setEditId(row.id); setName(row.name); setDesc(row.description); setModalOpen(true); }}
          >
            编辑
          </Button>
          <Popconfirm title="确认删除？" onConfirm={() => handleDelete(row.id)}>
            <Button type="link" danger size="small">删除</Button>
          </Popconfirm>
        </>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditId(null); setName(""); setDesc(""); setModalOpen(true); }} style={{ marginBottom: 16 }}>
        添加分类
      </Button>
      <Table dataSource={categories} columns={columns} rowKey="id" pagination={false} size="middle" />
      <Modal
        title={editId ? "编辑分类" : "添加分类"}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => setModalOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 16 }}>
          <Input placeholder="分类名称" value={name} onChange={(e) => setName(e.target.value)} />
          <Input placeholder="描述（可选）" value={desc} onChange={(e) => setDesc(e.target.value)} />
        </div>
      </Modal>
    </div>
  );
}
