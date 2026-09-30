import axios from "axios";
import type {
  ApprovalListResponse,
  BudgetWithSpent,
  Budget,
  Category,
  MonthlySummary,
  TrendPoint,
  BudgetAlert,
  SyncLog,
  UserInfo,
  VoucherAttachment,
  VoucherBatch,
  VoucherItemListResponse,
  VoucherIncompleteResponse,
  VoucherMatchCandidate,
  VoucherReceiptPoolItem,
  VoucherReceiptInvoiceCandidate,
  VoucherReceiptAutoMatchResult,
  VoucherReceiptClearResult,
  VoucherReceiptUploadResult,
  VoucherFeishuBatchAttachmentImportResult,
  VoucherFeishuAttachmentImportResult,
  VoucherFeishuCommentSyncResult,
  VoucherMatchConfirmResult,
  VoucherSourceAttachmentResponse,
} from "../types";

const api = axios.create({ baseURL: "/api/v1" });

// Token 管理
export function setToken(token: string | null) {
  if (token) {
    localStorage.setItem("expense-empty-demo-token", token);
    api.defaults.headers.common["Authorization"] = `Bearer ${token}`;
  } else {
    localStorage.removeItem("expense-empty-demo-token");
    delete api.defaults.headers.common["Authorization"];
  }
}

// 启动时恢复 token
const savedToken = localStorage.getItem("expense-empty-demo-token");
if (savedToken) {
  api.defaults.headers.common["Authorization"] = `Bearer ${savedToken}`;
}

// 401 自动跳转登录
api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      setToken(null);
      window.location.href = "/";
    }
    return Promise.reject(err);
  }
);

// Auth
export const login = (username: string, password: string) =>
  api.post<{ access_token: string }>("/auth/login", { username, password }).then((r) => r.data);
export const getMe = () => api.get<UserInfo>("/auth/me").then((r) => r.data);

// Users (admin only)
export const listUsers = () => api.get<UserInfo[]>("/users").then((r) => r.data);
export const createUser = (data: { username: string; password: string; display_name: string; role: string; category_ids: number[] }) =>
  api.post<UserInfo>("/users", data).then((r) => r.data);
export const updateUser = (id: number, data: Record<string, unknown>) =>
  api.put<UserInfo>(`/users/${id}`, data).then((r) => r.data);
export const deleteUser = (id: number) => api.delete(`/users/${id}`);

// Categories
export const listCategories = () => api.get<Category[]>("/categories").then((r) => r.data);
export const createCategory = (data: { name: string; description?: string }) =>
  api.post<Category>("/categories", data).then((r) => r.data);
export const updateCategory = (id: number, data: { name?: string; description?: string; sort_order?: number }) =>
  api.put<Category>(`/categories/${id}`, data).then((r) => r.data);
export const deleteCategory = (id: number) => api.delete(`/categories/${id}`);

// Approvals
export const listApprovals = (params: Record<string, unknown>) =>
  api.get<ApprovalListResponse>("/approvals", { params }).then((r) => r.data);
export const updateApprovalCategory = (id: number, category_id: number) =>
  api.put(`/approvals/${id}/category`, { category_id }).then((r) => r.data);
export const updateApprovalCurrency = (id: number, currency: string) =>
  api.put(`/approvals/${id}/currency`, { currency }).then((r) => r.data);
export const detectCurrency = () =>
  api.post("/approvals/detect-currency").then((r) => r.data);
export const listAirwallex = (params: Record<string, unknown>) =>
  api.get("/approvals/airwallex", { params }).then((r) => r.data);
export const updateAirwallexCategory = (id: number, category_id: number) =>
  api.put(`/approvals/airwallex/${id}/category`, { category_id }).then((r) => r.data);
export const listComments = (approvalId: number) =>
  api.get(`/approvals/${approvalId}/comments`).then((r) => r.data);
export const addComment = (approvalId: number, content: string) =>
  api.post(`/approvals/${approvalId}/comments`, { content }).then((r) => r.data);
export const resolveComment = (commentId: number) =>
  api.put(`/approvals/comments/${commentId}/resolve`).then((r) => r.data);
export const getPendingFeedbackCount = () =>
  api.get("/approvals/feedback/pending-count").then((r) => r.data.count as number);

// Budgets
export const listBudgets = (year_month: string) =>
  api.get<BudgetWithSpent[]>("/budgets", { params: { year_month } }).then((r) => r.data);
export const createBudget = (data: { year_month: string; category_id: number; amount: number; note?: string }) =>
  api.post<Budget>("/budgets", data).then((r) => r.data);
export const updateBudget = (id: number, data: { amount?: number; note?: string }) =>
  api.put<Budget>(`/budgets/${id}`, data).then((r) => r.data);
export const deleteBudget = (id: number) => api.delete(`/budgets/${id}`);
export const copyBudgetFromPrevious = (year_month: string) =>
  api.post("/budgets/copy-from-previous", null, { params: { year_month } }).then((r) => r.data);

// Dashboard
// 旧服务器统计接口异常时，使用仍可用的明细/预算接口在前端回退计算，保证看板可用。
const approvalMonth = (offset: number) => {
  const date = new Date();
  date.setDate(1);
  date.setMonth(date.getMonth() - offset);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
};

async function fallbackMonthlySummary(year_month: string): Promise<MonthlySummary> {
  const [approvals, budgets] = await Promise.all([
    listApprovals({ year_month, page: 1, page_size: 100 }),
    listBudgets(year_month),
  ]);
  const budgetByCategory = new Map(budgets.map((item) => [item.category_id, item.amount]));
  const grouped = new Map<number | null, { category_name: string; amount: number; count: number }>();
  for (const item of approvals.items) {
    const categoryId = item.category_id;
    const current = grouped.get(categoryId) || {
      category_name: item.category_name || "未分类",
      amount: 0,
      count: 0,
    };
    current.amount += item.amount_cny ?? item.amount ?? 0;
    current.count += 1;
    grouped.set(categoryId, current);
  }
  const totalBudget = budgets.reduce((sum, item) => sum + item.amount, 0);
  const totalAmount = approvals.sum_amount;
  return {
    year_month,
    total_amount: totalAmount,
    approved_count: approvals.items.filter((item) => item.status === 2).length,
    pending_count: approvals.items.filter((item) => item.status === 1).length,
    total_budget: totalBudget,
    budget_usage_pct: totalBudget > 0 ? Math.round(totalAmount / totalBudget * 1000) / 10 : 0,
    by_category: Array.from(grouped, ([category_id, item]) => {
      const budget = category_id == null ? null : budgetByCategory.get(category_id) ?? null;
      return {
        category_id,
        category_name: item.category_name,
        amount: Math.round(item.amount * 100) / 100,
        count: item.count,
        budget,
        usage_pct: budget ? Math.round(item.amount / budget * 1000) / 10 : null,
      };
    }),
    by_source: {
      feishu_amount: totalAmount,
      feishu_count: approvals.total,
      airwallex_amount: 0,
      airwallex_count: 0,
    },
  };
}

export const getMonthlySummary = async (year_month: string) => {
  try {
    return await api.get<MonthlySummary>("/dashboard/summary", { params: { year_month } }).then((r) => r.data);
  } catch {
    return fallbackMonthlySummary(year_month);
  }
};
export const getExpenseTrend = async (months = 6) => {
  try {
    return await api.get<TrendPoint[]>("/dashboard/trend", { params: { months } }).then((r) => r.data);
  } catch {
    const yearMonths = Array.from({ length: months }, (_, index) => approvalMonth(months - index - 1));
    const totals = await Promise.all(yearMonths.map((year_month) =>
      listApprovals({ year_month, page: 1, page_size: 1 }).then((result) => result.sum_amount)
    ));
    return yearMonths.map((year_month, index) => ({ year_month, total: totals[index] }));
  }
};
export const getBudgetAlerts = async (year_month: string) => {
  try {
    return await api.get<BudgetAlert[]>("/dashboard/budget-alerts", { params: { year_month } }).then((r) => r.data);
  } catch {
    return listBudgets(year_month).then((budgets) => budgets
      .filter((item) => item.usage_pct >= 80)
      .map((item) => ({
        category_id: item.category_id,
        category_name: item.category_name,
        budget: item.amount,
        spent: item.spent,
        usage_pct: item.usage_pct,
      }))
      .sort((a, b) => b.usage_pct - a.usage_pct));
  }
};
export const getUncategorized = (year_month?: string) =>
  api.get("/dashboard/uncategorized", { params: { year_month } }).then((r) => r.data);

// Sync
export const triggerSync = (topics?: number[]) =>
  api.post("/sync/trigger", topics ? { topics } : {}).then((r) => r.data);
export const listSyncLogs = () => api.get<SyncLog[]>("/sync/logs").then((r) => r.data);
export const importAirwallexExcel = (file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api.post("/sync/airwallex-import-excel", form).then((r) => r.data);
};

// Voucher binding (admin only, kept independent from existing approval rules)
export const listVoucherBatches = () =>
  api.get<VoucherBatch[]>("/vouchers/batches").then((r) => r.data);
export const listVoucherIncomplete = () =>
  api.get<VoucherIncompleteResponse>("/vouchers/incomplete").then((r) => r.data);
export const getVoucherBatchByMonth = (year_month: string) =>
  api.get<VoucherBatch>("/vouchers/batches/by-month", { params: { year_month } }).then((r) => r.data);
export const createOrSyncVoucherBatch = (year_month: string, syncFeishu = false) =>
  api.post<VoucherBatch>("/vouchers/batches", { year_month, sync_feishu: syncFeishu }).then((r) => r.data);
export const importVoucherBatchFeishuAttachments = (
  batchId: number,
  rescanExisting = false,
) =>
  api.post<VoucherFeishuBatchAttachmentImportResult>(
    `/vouchers/batches/${batchId}/import-feishu-attachments`,
    undefined,
    { params: { rescan_existing: rescanExisting } },
  ).then((r) => r.data);
export const getVoucherBatch = (id: number) =>
  api.get<VoucherBatch>(`/vouchers/batches/${id}`).then((r) => r.data);
export const listVoucherItems = (
  batchId: number,
  params: { page: number; page_size: number; status?: string; keyword?: string },
) => api.get<VoucherItemListResponse>(`/vouchers/batches/${batchId}/items`, { params }).then((r) => r.data);
export const uploadVoucherReceipts = (batchId: number, form: FormData) =>
  api.post<VoucherReceiptUploadResult>(`/vouchers/batches/${batchId}/receipts`, form).then((r) => r.data);
export const clearVoucherBatchReceipts = (batchId: number) =>
  api.delete<VoucherReceiptClearResult>(
    `/vouchers/batches/${batchId}/receipts`,
  ).then((r) => r.data);
export const autoMatchVoucherReceipts = (batchId: number) =>
  api.post<VoucherReceiptAutoMatchResult>(`/vouchers/batches/${batchId}/receipts/auto-match`).then((r) => r.data);
export const listVoucherReceiptPool = (batchId: number) =>
  api.get<VoucherReceiptPoolItem[]>(`/vouchers/batches/${batchId}/receipts`).then((r) => r.data);
export const uploadVoucherReceiptInvoice = (receiptDocumentId: number, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<{
    ok: boolean;
    deduplicated: boolean;
    receipt_document_id: number;
    invoice_attachment: VoucherAttachment;
  }>(`/vouchers/documents/${receiptDocumentId}/invoices`, form).then((r) => r.data);
};
export const removeVoucherReceiptInvoice = (
  receiptDocumentId: number,
  invoiceDocumentId: number,
) => api.delete(
  `/vouchers/documents/${receiptDocumentId}/invoices/${invoiceDocumentId}`,
).then((r) => r.data);
export const listVoucherReceiptInvoiceCandidates = (
  receiptDocumentId: number,
  keyword?: string,
) => api.get<VoucherReceiptInvoiceCandidate[]>(
  `/vouchers/documents/${receiptDocumentId}/invoice-candidates`,
  { params: { keyword: keyword || undefined } },
).then((r) => r.data);
export const referenceVoucherReceiptInvoices = (
  receiptDocumentId: number,
  documentIds: number[],
) => api.post<{
  ok: boolean;
  linked: number;
  skipped: number;
  invoice_attachments: VoucherAttachment[];
}>(
  `/vouchers/documents/${receiptDocumentId}/invoice-references`,
  { document_ids: documentIds },
).then((r) => r.data);
export const updateVoucherReceiptCategory = (
  documentId: number,
  category: "pending" | "income" | "other",
) => api.patch(`/vouchers/documents/${documentId}/receipt-category`, { category }).then((r) => r.data);
export const downloadVoucherReceiptPoolPdf = (batchId: number, documentIds: number[]) =>
  api.post<Blob>(
    `/vouchers/batches/${batchId}/receipts/export`,
    { document_ids: documentIds },
    { responseType: "blob" },
  ).then((r) => r.data);
export const uploadVoucherInvoice = (itemId: number, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<{ document_id: number; link_id: number; deduplicated: boolean }>(
    `/vouchers/items/${itemId}/invoice`,
    form,
  ).then((r) => r.data);
};
export const listVoucherMatches = (itemId: number) =>
  api.get<VoucherMatchCandidate[]>(`/vouchers/items/${itemId}/matches`).then((r) => r.data);
export const getVoucherSourceAttachments = (itemId: number) =>
  api.get<VoucherSourceAttachmentResponse>(`/vouchers/items/${itemId}/source-attachments`).then((r) => r.data);
export const importVoucherFeishuAttachments = (itemId: number) =>
  api.post<VoucherFeishuAttachmentImportResult>(
    `/vouchers/items/${itemId}/import-feishu-attachments`,
  ).then((r) => r.data);
export const updateVoucherRequirement = (
  itemId: number,
  requiresVoucher: boolean,
  reason?: string,
) =>
  api.patch(`/vouchers/items/${itemId}/requirement`, {
    requires_voucher: requiresVoucher,
    reason,
  }).then((r) => r.data);
export const updateVoucherMonthlyApproval = (itemId: number, includeApproval: boolean) =>
  api.patch<{
    ok: boolean;
    item_id: number;
    include_approval_in_monthly_pdf: boolean;
  }>(`/vouchers/items/${itemId}/monthly-approval`, {
    include_approval: includeApproval,
  }).then((r) => r.data);
export const updateVoucherInvoicePrinted = (
  itemId: number,
  documentId: number,
  printed: boolean,
) => api.patch<{
  ok: boolean;
  item_id: number;
  document_id: number;
  printed: boolean;
}>(`/vouchers/items/${itemId}/links/${documentId}/printed`, {
  printed,
}).then((r) => r.data);
export const updateVoucherBatchInvoicesPrinted = (batchId: number, printed: boolean) =>
  api.patch<{
    ok: boolean;
    batch_id: number;
    printed: boolean;
    updated: number;
    total: number;
    affected_items: number;
  }>(`/vouchers/batches/${batchId}/invoices/printed`, { printed }).then((r) => r.data);
export const updateVoucherItemInvoicesPrinted = (itemId: number, printed: boolean) =>
  api.patch<{
    ok: boolean;
    item_id: number;
    printed: boolean;
    updated: number;
    total: number;
  }>(`/vouchers/items/${itemId}/invoices/printed`, { printed }).then((r) => r.data);
export const confirmVoucherMatch = (itemId: number, candidateId: number) =>
  api.post<VoucherMatchConfirmResult>(
    `/vouchers/items/${itemId}/matches/${candidateId}/confirm`,
  ).then((r) => r.data);
export const syncVoucherReceiptInvoiceComments = (itemId: number) =>
  api.post<VoucherFeishuCommentSyncResult>(
    `/vouchers/items/${itemId}/receipt-invoices/sync-feishu-comments`,
  ).then((r) => r.data);
export const unlinkVoucherReceipt = (itemId: number) =>
  api.delete(`/vouchers/items/${itemId}/match`).then((r) => r.data);
export const unlinkVoucherDocument = (itemId: number, documentId: number) =>
  api.delete(`/vouchers/items/${itemId}/links/${documentId}`).then((r) => r.data);
export const deleteVoucherDocument = (documentId: number) =>
  api.delete(`/vouchers/documents/${documentId}`).then((r) => r.data);
export const downloadVoucherDocument = (documentId: number) =>
  api.get<Blob>(`/vouchers/documents/${documentId}/download`, { responseType: "blob" }).then((r) => r.data);
export const downloadVoucherItemPdf = (itemId: number) =>
  api.get<Blob>(`/vouchers/items/${itemId}/pdf`, { responseType: "blob" }).then((r) => r.data);
export const downloadVoucherBatchPdf = (batchId: number) =>
  api.get<Blob>(`/vouchers/batches/${batchId}/pdf`, { responseType: "blob" }).then((r) => r.data);
export const downloadVoucherBatchZip = (batchId: number) =>
  api.get<Blob>(`/vouchers/batches/${batchId}/zip`, { responseType: "blob" }).then((r) => r.data);
