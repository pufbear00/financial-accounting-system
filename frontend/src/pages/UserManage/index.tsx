import { useEffect, useState } from "react";
import { Table, Button, Modal, Input, Select, Tag, message, Popconfirm } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { listUsers, createUser, updateUser, deleteUser, listCategories } from "../../api/client";
import type { UserInfo, Category } from "../../types";

export default function UserManage() {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [form, setForm] = useState({ username: "", password: "", display_name: "", role: "viewer", category_ids: [] as number[], feishu_open_id: "" });

  const load = () => {
    listUsers().then(setUsers).catch(() => message.error("加载用户失败"));
    listCategories().then(setCategories).catch(() => {});
  };
  useEffect(() => { load(); }, []);

  const handleSave = async () => {
    try {
      if (editId) {
        const data: Record<string, unknown> = { display_name: form.display_name, role: form.role, category_ids: form.category_ids };
        if (form.password) data.password = form.password;
        await updateUser(editId, data);
      } else {
        await createUser(form);
      }
      setModalOpen(false);
      load();
      message.success("保存成功");
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(err.response?.data?.detail || "保存失败");
    }
  };

  const openCreate = () => {
    setEditId(null);
    setForm({ username: "", password: "", display_name: "", role: "viewer", category_ids: [], feishu_open_id: "" });
    setModalOpen(true);
  };

  const openEdit = (u: UserInfo) => {
    setEditId(u.id);
    setForm({ username: u.username, password: "", display_name: u.display_name, role: u.role, category_ids: u.category_ids, feishu_open_id: u.feishu_open_id || "" });
    setModalOpen(true);
  };

  const columns = [
    { title: "用户名", dataIndex: "username", width: 120 },
    { title: "显示名", dataIndex: "display_name", width: 120 },
    {
      title: "角色",
      dataIndex: "role",
      width: 100,
      render: (v: string) => <Tag color={v === "admin" ? "gold" : "blue"}>{v === "admin" ? "管理员" : "查看者"}</Tag>,
    },
    {
      title: "可见分类",
      dataIndex: "category_ids",
      render: (ids: number[], row: UserInfo) =>
        row.role === "admin" ? <Tag>全部</Tag> :
        ids.map((id) => <Tag key={id}>{categories.find((c) => c.id === id)?.name || id}</Tag>),
    },
    {
      title: "操作",
      width: 140,
      render: (_: unknown, row: UserInfo) => (
        <>
          <Button type="link" size="small" onClick={() => openEdit(row)}>编辑</Button>
          <Popconfirm title="确认删除？" onConfirm={() => deleteUser(row.id).then(load).catch((e: unknown) => {
            const err = e as { response?: { data?: { detail?: string } } };
            message.error(err.response?.data?.detail || "删除失败");
          })}>
            <Button type="link" danger size="small">删除</Button>
          </Popconfirm>
        </>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} style={{ marginBottom: 16 }}>
        添加用户
      </Button>
      <Table dataSource={users} columns={columns} rowKey="id" pagination={false} size="middle" />
      <Modal title={editId ? "编辑用户" : "添加用户"} open={modalOpen} onOk={handleSave} onCancel={() => setModalOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 16 }}>
          <Input
            placeholder="用户名"
            value={form.username}
            disabled={!!editId}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
          />
          <Input.Password
            placeholder={editId ? "新密码（留空不修改）" : "密码"}
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          <Input
            placeholder="显示名"
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          />
          <Select
            value={form.role}
            onChange={(v) => setForm({ ...form, role: v })}
            options={[
              { label: "管理员（全部可见）", value: "admin" },
              { label: "查看者（按分类）", value: "viewer" },
            ]}
          />
          {form.role === "viewer" && (
            <Select
              mode="multiple"
              placeholder="可见费用分类"
              value={form.category_ids}
              onChange={(v) => setForm({ ...form, category_ids: v })}
              options={categories.map((c) => ({ label: c.name, value: c.id }))}
            />
          )}
          <Input
            placeholder="飞书 Open ID（用于接收日报推送）"
            value={form.feishu_open_id}
            onChange={(e) => setForm({ ...form, feishu_open_id: e.target.value })}
          />
        </div>
      </Modal>
    </div>
  );
}
