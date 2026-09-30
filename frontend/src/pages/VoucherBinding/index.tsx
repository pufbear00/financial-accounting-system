import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Input,
  InputNumber,
  List,
  message,
  Modal,
  Pagination,
  Popconfirm,
  Progress,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  BankOutlined,
  CloudDownloadOutlined,
  DeleteOutlined,
  DownOutlined,
  DownloadOutlined,
  FileAddOutlined,
  FilePdfOutlined,
  FileSearchOutlined,
  InboxOutlined,
  LinkOutlined,
  PlusOutlined,
  PrinterOutlined,
  ReloadOutlined,
  StopOutlined,
  UpOutlined,
  UploadOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import {
  autoMatchVoucherReceipts,
  clearVoucherBatchReceipts,
  confirmVoucherMatch,
  createOrSyncVoucherBatch,
  deleteVoucherDocument,
  downloadVoucherBatchPdf,
  downloadVoucherBatchZip,
  downloadVoucherDocument,
  downloadVoucherItemPdf,
  downloadVoucherReceiptPoolPdf,
  getVoucherBatch,
  getVoucherBatchByMonth,
  getVoucherSourceAttachments,
  importVoucherBatchFeishuAttachments,
  importVoucherFeishuAttachments,
  listVoucherIncomplete,
  listVoucherItems,
  listVoucherMatches,
  listVoucherReceiptInvoiceCandidates,
  listVoucherReceiptPool,
  referenceVoucherReceiptInvoices,
  removeVoucherReceiptInvoice,
  syncVoucherReceiptInvoiceComments,
  unlinkVoucherDocument,
  updateVoucherMonthlyApproval,
  updateVoucherBatchInvoicesPrinted,
  updateVoucherItemInvoicesPrinted,
  updateVoucherReceiptCategory,
  updateVoucherRequirement,
  uploadVoucherInvoice,
  uploadVoucherReceiptInvoice,
  uploadVoucherReceipts,
} from "../../api/client";
import type {
  VoucherAttachment,
  VoucherBatch,
  VoucherIncompleteItem,
  VoucherIncompleteResponse,
  VoucherItem,
  VoucherMatchCandidate,
  VoucherReceiptInvoiceCandidate,
  VoucherReceiptPoolItem,
  VoucherSourceAttachment,
} from "../../types";
import "./style.css";

const { Text, Title } = Typography;

type ReceiptUploadRow = {
  key: string;
  file: File;
  amount?: number;
  payee_name: string;
  transaction_date: string;
};

type VoucherSyncOptions = {
  yearMonth: string;
  requestId: number;
  interactive: boolean;
  existed: boolean;
};

type VoucherItemsQuerySnapshot = {
  yearMonth: string;
  batchId: number;
  page: number;
  statusFilter: string;
  keyword: string;
};

type IncompleteJumpTarget = {
  yearMonth: string;
  itemId: number;
  voucherNo: string;
};

type ReceiptPoolCategory = "pending" | "income" | "referenced";

const PAGE_SIZE = 10;
const acceptedFiles = ".pdf,.jpg,.jpeg,.png";
const VOUCHER_START_MONTH = dayjs("2026-07-01").startOf("month");

function getInitialVoucherMonth() {
  const current = dayjs().startOf("month");
  return current.isBefore(VOUCHER_START_MONTH, "month")
    ? VOUCHER_START_MONTH
    : current;
}

function getErrorMessage(error: unknown, fallback = "操作失败") {
  const value = error as { response?: { data?: { detail?: string } }; message?: string };
  return value.response?.data?.detail || value.message || fallback;
}

async function getDownloadErrorMessage(error: unknown, fallback = "文件生成失败") {
  const value = error as {
    response?: { data?: Blob | { detail?: string } };
    message?: string;
  };
  const data = value.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text()) as { detail?: string };
      if (parsed.detail) return parsed.detail;
    } catch {
      // Non-JSON bodies fall through to the normal request error message.
    }
  } else if (data?.detail) {
    return data.detail;
  }
  return value.message || fallback;
}

function normalizeScore(score: number | null | undefined) {
  if (score == null || Number.isNaN(score)) return 0;
  return Math.max(0, Math.min(100, Math.round(score)));
}

function breakdownScore(value: number | { score?: number } | undefined) {
  return typeof value === "number" ? value : value?.score || 0;
}

function formatMoney(value: number | null | undefined, currency = "CNY") {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: currency || "CNY",
    maximumFractionDigits: 2,
  }).format(value || 0);
}

function formatSize(size: number | null | undefined) {
  if (!size) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function invoiceAttachments(item: VoucherItem) {
  if (item.invoice_attachments?.length) return item.invoice_attachments;
  return item.invoice_attachment ? [item.invoice_attachment] : [];
}

function receiptAttachments(item: VoucherItem) {
  if (item.receipt_attachments?.length) return item.receipt_attachments;
  return item.receipt_attachment ? [item.receipt_attachment] : [];
}

function itemComplete(item: VoucherItem) {
  return item.requires_voucher && invoiceAttachments(item).length > 0 && receiptAttachments(item).length > 0;
}

function candidateReceiptName(candidate: VoucherMatchCandidate) {
  return typeof candidate.receipt === "string"
    ? candidate.receipt
    : candidate.receipt?.file_name || `回单 #${candidate.id}`;
}

function receiptHasApprovalReference(receipt: VoucherReceiptPoolItem) {
  return (receipt.invoice_attachments || []).some(
    (invoice) => invoice.referenced_from_approval === true,
  );
}

function receiptMatchesPoolCategory(
  receipt: VoucherReceiptPoolItem,
  category: ReceiptPoolCategory,
) {
  const referenced = receiptHasApprovalReference(receipt);
  if (category === "referenced") return referenced;
  if (referenced) return false;
  if (category === "income") return receipt.receipt_category === "income";
  return (receipt.receipt_category || "pending") !== "income";
}

function receiptPoolCategoryTitle(category: ReceiptPoolCategory) {
  if (category === "income") return "收入";
  if (category === "referenced") return "已引用";
  return "待匹配及其他";
}

export default function VoucherBinding() {
  const [messageApi, messageContext] = message.useMessage();
  const [modalApi, modalContext] = Modal.useModal();
  const [month, setMonth] = useState<Dayjs>(getInitialVoucherMonth);
  const [batch, setBatch] = useState<VoucherBatch | null>(null);
  const [items, setItems] = useState<VoucherItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState("all");
  const [keyword, setKeyword] = useState("");
  const [searchText, setSearchText] = useState("");
  const [batchLoading, setBatchLoading] = useState(false);
  const [tableLoading, setTableLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [exporting, setExporting] = useState<string | null>(null);
  const [incompleteOpen, setIncompleteOpen] = useState(false);
  const [incompleteLoading, setIncompleteLoading] = useState(false);
  const [incompleteJumpTarget, setIncompleteJumpTarget] = useState<IncompleteJumpTarget | null>(null);
  const [highlightedVoucherItemId, setHighlightedVoucherItemId] = useState<number | null>(null);
  const [loadedItemsQuery, setLoadedItemsQuery] = useState<VoucherItemsQuerySnapshot | null>(null);
  const [incompleteData, setIncompleteData] = useState<VoucherIncompleteResponse>({
    total: 0,
    missing_invoice: 0,
    missing_receipt: 0,
    months: [],
  });
  const [monthlyApprovalUpdating, setMonthlyApprovalUpdating] = useState<Set<number>>(
    () => new Set(),
  );
  const [batchInvoicesPrintedUpdating, setBatchInvoicesPrintedUpdating] = useState(false);
  const [itemInvoicesPrintedUpdating, setItemInvoicesPrintedUpdating] = useState<Set<number>>(
    () => new Set(),
  );
  const [expandedExpenseDetailIds, setExpandedExpenseDetailIds] = useState<Set<number>>(
    () => new Set(),
  );
  const batchLoadRequestRef = useRef(0);
  const itemLoadRequestRef = useRef(0);
  const candidateRequestRef = useRef(0);
  const lastLiveRefreshRef = useRef(0);
  const incompleteJumpScrollTimerRef = useRef<number | null>(null);
  const incompleteJumpHighlightTimerRef = useRef<number | null>(null);
  const syncInFlightRef = useRef<{
    yearMonth: string;
    requestId: number;
    promise: Promise<void>;
  } | null>(null);

  const receiptInputRef = useRef<HTMLInputElement>(null);
  const [receiptUploadOpen, setReceiptUploadOpen] = useState(false);
  const [receiptRows, setReceiptRows] = useState<ReceiptUploadRow[]>([]);
  const [receiptUploading, setReceiptUploading] = useState(false);

  const [candidateOpen, setCandidateOpen] = useState(false);
  const [candidateItem, setCandidateItem] = useState<VoucherItem | null>(null);
  const [candidates, setCandidates] = useState<VoucherMatchCandidate[]>([]);
  const [candidateSearch, setCandidateSearch] = useState("");
  const [candidateLoading, setCandidateLoading] = useState(false);
  const [confirmingCandidate, setConfirmingCandidate] = useState<number | null>(null);

  const [receiptPoolOpen, setReceiptPoolOpen] = useState(false);
  const [receiptPool, setReceiptPool] = useState<VoucherReceiptPoolItem[]>([]);
  const [receiptPoolLoading, setReceiptPoolLoading] = useState(false);
  const [selectedReceiptPoolIds, setSelectedReceiptPoolIds] = useState<Set<number>>(
    () => new Set(),
  );
  const [receiptPoolExporting, setReceiptPoolExporting] = useState(false);
  const [receiptPoolCategory, setReceiptPoolCategory] = useState<ReceiptPoolCategory>("pending");
  const [receiptPoolSearch, setReceiptPoolSearch] = useState("");
  const [receiptCategoryUpdating, setReceiptCategoryUpdating] = useState<Set<number>>(
    () => new Set(),
  );
  const [receiptAutoMatching, setReceiptAutoMatching] = useState(false);
  const [receiptClearing, setReceiptClearing] = useState(false);
  const [receiptInvoiceUploading, setReceiptInvoiceUploading] = useState<Set<number>>(
    () => new Set(),
  );
  const [receiptInvoiceRemoving, setReceiptInvoiceRemoving] = useState<Set<number>>(
    () => new Set(),
  );
  const receiptInvoiceUploadingRef = useRef<Set<number>>(new Set());
  const invoiceReferenceRequestRef = useRef(0);
  const [invoiceReferenceOpen, setInvoiceReferenceOpen] = useState(false);
  const [invoiceReferenceReceipt, setInvoiceReferenceReceipt] = useState<VoucherReceiptPoolItem | null>(null);
  const [invoiceReferenceCandidates, setInvoiceReferenceCandidates] = useState<VoucherReceiptInvoiceCandidate[]>([]);
  const [invoiceReferenceSearch, setInvoiceReferenceSearch] = useState("");
  const [invoiceReferencePage, setInvoiceReferencePage] = useState(1);
  const [invoiceReferenceSelectedIds, setInvoiceReferenceSelectedIds] = useState<Set<number>>(
    () => new Set(),
  );
  const [invoiceReferenceLoading, setInvoiceReferenceLoading] = useState(false);
  const [invoiceReferenceSubmitting, setInvoiceReferenceSubmitting] = useState(false);

  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceItem, setSourceItem] = useState<VoucherItem | null>(null);
  const [sourceCandidates, setSourceCandidates] = useState<VoucherSourceAttachment[]>([]);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [feishuImportingItems, setFeishuImportingItems] = useState<Set<number>>(() => new Set());
  const [feishuCommentSyncingItems, setFeishuCommentSyncingItems] = useState<Set<number>>(
    () => new Set(),
  );

  const yearMonth = month.format("YYYY-MM");
  const visibleBatchId = batch?.year_month === yearMonth ? batch.id : null;
  const currentViewRef = useRef({
    yearMonth,
    batchId: visibleBatchId,
    page,
    statusFilter,
    keyword,
  });
  currentViewRef.current = {
    yearMonth,
    batchId: visibleBatchId,
    page,
    statusFilter,
    keyword,
  };
  const hasVoucherItems = Boolean(batch && batch.summary.required_total > 0);
  const filteredCandidates = useMemo(() => {
    const query = candidateSearch.trim().toLocaleLowerCase().replace(/[,，\s]/g, "");
    if (!query) return candidates;
    return candidates.filter((candidate) => [
      candidateReceiptName(candidate),
      candidate.amount,
      candidate.payee_name,
      candidate.transaction_date,
      candidate.bank_reference,
    ].some((value) => String(value ?? "")
      .toLocaleLowerCase()
      .replace(/[,，\s]/g, "")
      .includes(query)));
  }, [candidateSearch, candidates]);

  const filteredReceiptPool = useMemo(() => {
    const query = receiptPoolSearch
      .trim()
      .toLocaleLowerCase()
      .replace(/[,，￥¥$\s]/g, "");
    if (!query) return receiptPool;
    return receiptPool.filter((receipt) => [
      receipt.file_name,
      receipt.amount,
      receipt.amount == null ? null : formatMoney(receipt.amount),
      receipt.payee_name,
      receipt.transaction_date,
      receipt.bank_reference,
      ...(receipt.invoice_attachments || []).map((invoice) => invoice.file_name),
    ].some((value) => String(value ?? "")
      .toLocaleLowerCase()
      .replace(/[,，￥¥$\s]/g, "")
      .includes(query)));
  }, [receiptPool, receiptPoolSearch]);

  const filteredInvoiceReferenceCandidates = useMemo(() => {
    const query = invoiceReferenceSearch
      .trim()
      .toLocaleLowerCase()
      .replace(/[,，￥¥$\s]/g, "");
    if (!query) return invoiceReferenceCandidates;
    return invoiceReferenceCandidates.filter((candidate) => [
      candidate.year_month,
      candidate.voucher_no,
      candidate.instance_code,
      candidate.definition_name,
      candidate.applicant_name,
      candidate.payee_name,
      candidate.description,
      candidate.amount,
      candidate.amount == null ? null : formatMoney(candidate.amount, candidate.currency),
      candidate.invoice_attachment.file_name,
    ].some((value) => String(value ?? "")
      .toLocaleLowerCase()
      .replace(/[,，￥¥$\s]/g, "")
      .includes(query)));
  }, [invoiceReferenceCandidates, invoiceReferenceSearch]);

  const replaceReceiptPool = useCallback((rows: VoucherReceiptPoolItem[]) => {
    setReceiptPool(rows);
    const selectableIds = new Set(
      rows
        .filter((row) => (
          receiptHasApprovalReference(row)
          || row.receipt_category === "income"
          || row.receipt_category === "other"
        ))
        .map((row) => row.id),
    );
    setSelectedReceiptPoolIds((current) => new Set(
      Array.from(current).filter((id) => selectableIds.has(id)),
    ));
  }, []);

  const loadIncomplete = useCallback(async (showError = true) => {
    setIncompleteLoading(true);
    try {
      setIncompleteData(await listVoucherIncomplete());
    } catch (error) {
      if (showError) {
        messageApi.error(getErrorMessage(error, "累计不完整审批加载失败"));
      }
    } finally {
      setIncompleteLoading(false);
    }
  }, [messageApi]);

  const loadStoredBatch = useCallback(async (
    requestedMonth: string,
    requestId: number,
  ): Promise<VoucherBatch | null> => {
    setBatchLoading(true);
    try {
      let result: VoucherBatch;
      try {
        // Reading the saved batch keeps opening the page independent from
        // Feishu's network latency.
        result = await getVoucherBatchByMonth(requestedMonth);
      } catch (error) {
        const status = (error as { response?: { status?: number } }).response?.status;
        if (status !== 404) throw error;
        // A new month still gets a usable local batch immediately.  The full
        // Feishu refresh starts in the background after this request returns.
        result = await createOrSyncVoucherBatch(requestedMonth, false);
      }
      if (
        requestId !== batchLoadRequestRef.current
        || currentViewRef.current.yearMonth !== requestedMonth
      ) {
        return null;
      }
      setBatch(result);
      return result;
    } catch (error) {
      if (
        requestId === batchLoadRequestRef.current
        && currentViewRef.current.yearMonth === requestedMonth
      ) {
        messageApi.error(getErrorMessage(error, "读取装订批次失败"));
        setBatch(null);
        setItems([]);
        setTotal(0);
        setLoadedItemsQuery(null);
      }
      return null;
    } finally {
      if (
        requestId === batchLoadRequestRef.current
        && currentViewRef.current.yearMonth === requestedMonth
      ) {
        setBatchLoading(false);
      }
    }
  }, [messageApi]);

  const runFullSync = useCallback(async (options: VoucherSyncOptions): Promise<void> => {
    if (!options.interactive) return; // No automatic external refresh in the demo.
    setSyncing(true);
    try {
      const result = await createOrSyncVoucherBatch(options.yearMonth, false);
      setBatch(result);
      const rows = await listVoucherItems(result.id, { page: 1, page_size: PAGE_SIZE });
      setItems(rows.items);
      setTotal(rows.total);
      setPage(1);
      messageApi.success("已刷新本地数据；未连接任何外部审批系统");
    } catch (error) {
      messageApi.error(getErrorMessage(error, "本地刷新失败"));
    } finally {
      setSyncing(false);
    }
  }, [messageApi]);

  const batchId = visibleBatchId;
  const loadItems = useCallback(async () => {
    if (
      batchId === null
      || currentViewRef.current.yearMonth !== yearMonth
      || currentViewRef.current.batchId !== batchId
    ) {
      return;
    }
    const requestId = itemLoadRequestRef.current + 1;
    itemLoadRequestRef.current = requestId;
    const monthRequestId = batchLoadRequestRef.current;
    const requestedMonth = yearMonth;
    setTableLoading(true);
    try {
      const result = await listVoucherItems(batchId, {
        page,
        page_size: PAGE_SIZE,
        status: statusFilter === "all" ? undefined : statusFilter,
        keyword: keyword || undefined,
      });
      if (
        requestId !== itemLoadRequestRef.current
        || monthRequestId !== batchLoadRequestRef.current
        || currentViewRef.current.yearMonth !== requestedMonth
        || currentViewRef.current.batchId !== batchId
      ) {
        return;
      }
      setItems(result.items);
      setTotal(result.total);
      setLoadedItemsQuery({
        yearMonth: requestedMonth,
        batchId,
        page,
        statusFilter,
        keyword,
      });
    } catch (error) {
      if (requestId === itemLoadRequestRef.current) {
        messageApi.error(getErrorMessage(error, "读取凭证明细失败"));
      }
    } finally {
      if (requestId === itemLoadRequestRef.current) {
        setTableLoading(false);
      }
    }
  }, [batchId, keyword, messageApi, page, statusFilter, yearMonth]);

  const refresh = useCallback(async () => {
    if (
      batchId === null
      || currentViewRef.current.yearMonth !== yearMonth
      || currentViewRef.current.batchId !== batchId
    ) {
      return;
    }
    const monthRequestId = batchLoadRequestRef.current;
    const requestedMonth = yearMonth;
    try {
      const current = await getVoucherBatch(batchId);
      if (
        monthRequestId === batchLoadRequestRef.current
        && currentViewRef.current.yearMonth === requestedMonth
        && currentViewRef.current.batchId === batchId
      ) {
        setBatch(current);
      }
    } catch (error) {
      if (
        monthRequestId === batchLoadRequestRef.current
        && currentViewRef.current.yearMonth === requestedMonth
        && currentViewRef.current.batchId === batchId
      ) {
        messageApi.error(getErrorMessage(error, "刷新批次失败"));
      }
    }
  }, [batchId, messageApi, yearMonth]);

  const refreshAll = useCallback(async () => {
    await Promise.all([refresh(), loadItems(), loadIncomplete(false)]);
  }, [loadIncomplete, loadItems, refresh]);

  useEffect(() => {
    void loadIncomplete(false);
  }, [loadIncomplete]);

  useEffect(() => {
    const requestId = batchLoadRequestRef.current + 1;
    batchLoadRequestRef.current = requestId;
    itemLoadRequestRef.current += 1;
    lastLiveRefreshRef.current = Date.now();
    setPage(1);
    setStatusFilter("all");
    setKeyword("");
    setSearchText("");
    setExpandedExpenseDetailIds(new Set());
    setBatch(null);
    setItems([]);
    setTotal(0);
    setLoadedItemsQuery(null);
    setTableLoading(false);

    void (async () => {
      const storedBatch = await loadStoredBatch(yearMonth, requestId);
      if (
        storedBatch
        && requestId === batchLoadRequestRef.current
        && currentViewRef.current.yearMonth === yearMonth
      ) {
        // Do not await: the saved batch is already visible and remains usable
        // while Feishu approvals and attachments refresh in the background.
        void runFullSync({
          yearMonth,
          requestId,
          interactive: false,
          existed: true,
        });
      }
    })();
  }, [loadStoredBatch, runFullSync, yearMonth]);

  useEffect(() => {
    if (!incompleteJumpTarget || incompleteJumpTarget.yearMonth !== yearMonth) return;
    // The month-loading effect above resets the table query. Re-apply the
    // precise voucher query afterwards so cross-month and same-month jumps
    // follow the same path.
    setPage(1);
    setStatusFilter("all");
    setSearchText(incompleteJumpTarget.voucherNo);
    setKeyword(incompleteJumpTarget.voucherNo);
  }, [incompleteJumpTarget, yearMonth]);

  useEffect(() => {
    const refreshWhenActive = () => {
      const view = currentViewRef.current;
      if (
        document.visibilityState === "visible"
        && view.batchId !== null
        && Date.now() - lastLiveRefreshRef.current >= 120_000
      ) {
        void runFullSync({
          yearMonth: view.yearMonth,
          requestId: batchLoadRequestRef.current,
          interactive: false,
          existed: true,
        });
      }
    };
    window.addEventListener("focus", refreshWhenActive);
    document.addEventListener("visibilitychange", refreshWhenActive);
    const intervalId = window.setInterval(refreshWhenActive, 300_000);
    return () => {
      window.removeEventListener("focus", refreshWhenActive);
      document.removeEventListener("visibilitychange", refreshWhenActive);
      window.clearInterval(intervalId);
    };
  }, [runFullSync]);

  useEffect(() => {
    void loadItems();
  }, [loadItems]);

  useEffect(() => {
    const target = incompleteJumpTarget;
    if (
      !target
      || target.yearMonth !== yearMonth
      || batchId === null
      || tableLoading
      || loadedItemsQuery?.yearMonth !== target.yearMonth
      || loadedItemsQuery.batchId !== batchId
      || loadedItemsQuery.page !== 1
      || loadedItemsQuery.statusFilter !== "all"
      || loadedItemsQuery.keyword !== target.voucherNo
    ) {
      return;
    }

    const locatedItem = items.find((item) => item.id === target.itemId)
      || items.find((item) => item.voucher_no === target.voucherNo);
    setIncompleteJumpTarget(null);
    if (!locatedItem) {
      messageApi.warning(`未找到 ${target.voucherNo}，该审批可能已更新，请刷新累计清单后重试`);
      return;
    }

    if (incompleteJumpScrollTimerRef.current !== null) {
      window.clearTimeout(incompleteJumpScrollTimerRef.current);
    }
    if (incompleteJumpHighlightTimerRef.current !== null) {
      window.clearTimeout(incompleteJumpHighlightTimerRef.current);
    }
    setHighlightedVoucherItemId(locatedItem.id);
    // Wait for the drawer closing animation and the highlighted table row to
    // render before moving it into view.
    incompleteJumpScrollTimerRef.current = window.setTimeout(() => {
      document.getElementById(`voucher-item-${locatedItem.id}`)?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
      incompleteJumpScrollTimerRef.current = null;
    }, 320);
    incompleteJumpHighlightTimerRef.current = window.setTimeout(() => {
      setHighlightedVoucherItemId((current) => current === locatedItem.id ? null : current);
      incompleteJumpHighlightTimerRef.current = null;
    }, 3600);
  }, [batchId, incompleteJumpTarget, items, loadedItemsQuery, messageApi, tableLoading, yearMonth]);

  useEffect(() => () => {
    if (incompleteJumpScrollTimerRef.current !== null) {
      window.clearTimeout(incompleteJumpScrollTimerRef.current);
    }
    if (incompleteJumpHighlightTimerRef.current !== null) {
      window.clearTimeout(incompleteJumpHighlightTimerRef.current);
    }
  }, []);

  const handleCreateOrSync = async () => {
    await runFullSync({
      yearMonth,
      requestId: batchLoadRequestRef.current,
      interactive: true,
      existed: Boolean(batch),
    });
  };

  const openIncomplete = () => {
    setIncompleteOpen(true);
    void loadIncomplete();
  };

  const clearIncompleteJump = () => {
    setIncompleteJumpTarget(null);
    setHighlightedVoucherItemId(null);
    if (incompleteJumpScrollTimerRef.current !== null) {
      window.clearTimeout(incompleteJumpScrollTimerRef.current);
      incompleteJumpScrollTimerRef.current = null;
    }
    if (incompleteJumpHighlightTimerRef.current !== null) {
      window.clearTimeout(incompleteJumpHighlightTimerRef.current);
      incompleteJumpHighlightTimerRef.current = null;
    }
  };

  const openIncompleteMonth = (targetMonth: string) => {
    clearIncompleteJump();
    setIncompleteOpen(false);
    setPage(1);
    setStatusFilter("all");
    setKeyword("");
    setSearchText("");
    if (targetMonth !== yearMonth) {
      setMonth(dayjs(`${targetMonth}-01`).startOf("month"));
    }
  };

  const openIncompleteItem = (targetMonth: string, item: VoucherIncompleteItem) => {
    clearIncompleteJump();
    setIncompleteOpen(false);
    setIncompleteJumpTarget({
      yearMonth: targetMonth,
      itemId: item.item_id,
      voucherNo: item.voucher_no,
    });
    setPage(1);
    setStatusFilter("all");
    setSearchText(item.voucher_no);
    setKeyword(item.voucher_no);
    if (targetMonth !== yearMonth) {
      setMonth(dayjs(`${targetMonth}-01`).startOf("month"));
    }
  };

  const handleReceiptSelection = (files: FileList | null) => {
    if (!files?.length) return;
    const now = Date.now();
    const rows = Array.from(files).map((file, index) => ({
      key: `${now}-${index}-${file.name}`,
      file,
      payee_name: "",
      transaction_date: "",
    }));
    setReceiptRows(rows);
    setReceiptUploadOpen(true);
    if (receiptInputRef.current) receiptInputRef.current.value = "";
  };

  const updateReceiptRow = (key: string, patch: Partial<ReceiptUploadRow>) => {
    setReceiptRows((current) => current.map((row) => row.key === key ? { ...row, ...patch } : row));
  };

  const submitReceiptUpload = async () => {
    if (!batch || !receiptRows.length) return;
    const form = new FormData();
    receiptRows.forEach((row) => form.append("files", row.file));
    form.append("metadata_json", JSON.stringify(Object.fromEntries(receiptRows.map((row) => [
      row.file.name,
      {
        amount: row.amount ?? null,
        payee: row.payee_name.trim() || null,
        transaction_date: row.transaction_date || null,
      },
    ]))));
    setReceiptUploading(true);
    try {
      const result = await uploadVoucherReceipts(batch.id, form);
      messageApi.success(
        `上传 ${result.uploaded} 份，自动匹配 ${result.matched} 份，待人工确认 ${result.pending_confirmation} 份` +
        (result.split_pages ? `，已拆分 ${result.split_pages} 页` : "") +
        (result.duplicates ? `，重复 ${result.duplicates} 份` : "") +
        (Array.isArray(result.failed) && result.failed.length
          ? `，失败 ${result.failed.length} 份：${result.failed.map((item) => item.file_name).join("、")}`
          : typeof result.failed === "number" && result.failed ? `，失败 ${result.failed} 份` : ""),
      );
      setReceiptUploadOpen(false);
      setReceiptRows([]);
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "银行回单上传失败"));
    } finally {
      setReceiptUploading(false);
    }
  };

  const handleReceiptAutoMatch = async () => {
    if (!batch) return;
    setReceiptAutoMatching(true);
    try {
      const result = await autoMatchVoucherReceipts(batch.id);
      const summary = `已扫描 ${result.scanned} 张回单，自动匹配 ${result.matched} 张` +
        (result.pending_confirmation ? `，仍有 ${result.pending_confirmation} 张待人工确认` : "");
      messageApi.success(summary);
      await refreshAll();
      if (receiptPoolOpen) {
        replaceReceiptPool(await listVoucherReceiptPool(batch.id));
      }
    } catch (error) {
      messageApi.error(getErrorMessage(error, "自动匹配回单失败"));
    } finally {
      setReceiptAutoMatching(false);
    }
  };

  const handleClearReceipts = () => {
    if (
      !batch
      || receiptClearing
      || !batch.summary.receipt_total
      || batch.status === "archived"
    ) return;
    const targetBatchId = batch.id;
    const targetMonth = batch.year_month;
    const targetCount = batch.summary.receipt_total;
    modalApi.confirm({
      title: `确认清空 ${targetMonth} 的全部银行回单？`,
      content: (
        <Space orientation="vertical" size={4}>
          <Text>
            将删除本月已匹配、待匹配、收入和其他共 {targetCount} 份回单，并解除所有回单匹配。
          </Text>
          <Text type="danger">
            随回单添加的可选发票也会删除；审批单中原有发票和其他月份不受影响。清空后不可恢复。
          </Text>
        </Space>
      ),
      okText: "确认清空",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setReceiptClearing(true);
        try {
          const result = await clearVoucherBatchReceipts(targetBatchId);
          if (currentViewRef.current.batchId === targetBatchId) {
            setReceiptPool([]);
            setSelectedReceiptPoolIds(new Set());
            setCandidates([]);
            setCandidateItem(null);
            candidateRequestRef.current += 1;
            setCandidateLoading(false);
            setCandidateOpen(false);
            await refreshAll();
          }
          if (result.file_cleanup_failed) {
            messageApi.warning(
              `已清空 ${result.deleted_receipts} 份回单并解除 ${result.deleted_links} 处匹配；` +
              `${result.file_cleanup_failed} 个历史文件等待后台清理`,
            );
          } else {
            messageApi.success(
              `已清空 ${result.deleted_receipts} 份回单` +
              (result.deleted_invoices ? `及 ${result.deleted_invoices} 份随附发票` : "") +
              `，解除 ${result.deleted_links} 处匹配`,
            );
          }
        } catch (error) {
          messageApi.error(getErrorMessage(error, "清空本月回单失败"));
          throw error;
        } finally {
          setReceiptClearing(false);
        }
      },
    });
  };

  const handleInvoiceUpload = async (item: VoucherItem, file: File) => {
    try {
      const result = await uploadVoucherInvoice(item.id, file);
      messageApi.success(result.deduplicated ? "该发票已存在，已保留原文件" : "发票已添加");
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "发票上传失败"));
    }
  };

  const handleFeishuAttachmentImport = async (item: VoucherItem) => {
    setFeishuImportingItems((current) => new Set(current).add(item.id));
    try {
      const result = await importVoucherFeishuAttachments(item.id);
      if (result.linked === 0) {
        const summary =
          `飞书当前未返回新的 PDF/图片附件（扫描 ${result.scanned} 个，` +
          `重复 ${result.duplicates} 个，跳过 ${result.skipped} 个` +
          (result.failed.length ? `，失败 ${result.failed.length} 个` : "") +
          "）";
        if (result.failed.length > 0) {
          messageApi.warning(`${summary}，请重试或手动上传`);
        } else {
          messageApi.info(summary);
        }
      } else if (result.failed.length > 0) {
        messageApi.warning(
          `读取到 ${result.scanned} 个附件，关联 ${result.linked} 个，失败 ${result.failed.length} 个；请重试或手动上传`,
        );
      } else if (result.linked > 0) {
        messageApi.success(`已从飞书关联 ${result.linked} 个附件`);
      }
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "飞书附件读取失败"));
    } finally {
      setFeishuImportingItems((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  };

  const handleFeishuCommentSync = async (item: VoucherItem) => {
    setFeishuCommentSyncingItems((current) => new Set(current).add(item.id));
    try {
      const result = await syncVoucherReceiptInvoiceComments(item.id);
      if (!result.eligible) {
        messageApi.info("本笔没有随回单单独上传的发票");
      } else if (result.failed || result.uncertain || result.processing) {
        const reason = result.errors[0]?.reason;
        messageApi.warning(
          `飞书评论同步成功 ${result.synced} 张，` +
          `${result.failed + result.uncertain} 张待处理` +
          (result.processing ? `，${result.processing} 张同步中` : "") +
          (reason ? `：${reason}` : ""),
        );
      } else if (result.synced) {
        messageApi.success(`已将 ${result.synced} 张发票同步到飞书审批评论`);
      } else {
        messageApi.success("随回单发票已全部同步到飞书评论");
      }
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "飞书评论同步失败"));
    } finally {
      setFeishuCommentSyncingItems((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  };

  const handleDocumentDownload = async (attachment: Pick<VoucherAttachment, "id" | "file_name">) => {
    try {
      const blob = await downloadVoucherDocument(attachment.id);
      saveBlob(blob, attachment.file_name);
    } catch (error) {
      messageApi.error(getErrorMessage(error, "附件下载失败"));
    }
  };

  const handleInvoiceUnlink = async (item: VoucherItem, attachment: VoucherAttachment) => {
    try {
      await unlinkVoucherDocument(item.id, attachment.id);
      messageApi.success("发票已从本凭证移除");
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "移除发票失败"));
    }
  };

  const handleRequirementChange = async (item: VoucherItem, requiresVoucher: boolean) => {
    try {
      await updateVoucherRequirement(
        item.id,
        requiresVoucher,
        requiresVoucher ? undefined : "人工标记无需装订",
      );
      messageApi.success(requiresVoucher ? "已恢复为需要装订" : "已标记为无需装订");
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "更新装订状态失败"));
    }
  };

  const handleMonthlyApprovalChange = async (item: VoucherItem) => {
    const includeApproval = item.include_approval_in_monthly_pdf === false;
    setMonthlyApprovalUpdating((current) => new Set(current).add(item.id));
    try {
      await updateVoucherMonthlyApproval(item.id, includeApproval);
      messageApi.success(
        includeApproval
          ? "已将这笔审批单特殊加入月度 PDF"
          : "已恢复默认：月度 PDF 不含这笔审批单",
      );
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "更新月度审批单设置失败"));
    } finally {
      setMonthlyApprovalUpdating((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  };

  const handleBatchInvoicesPrintedChange = async (printed: boolean) => {
    if (!batch || batchInvoicesPrintedUpdating) return;
    setBatchInvoicesPrintedUpdating(true);
    try {
      const result = await updateVoucherBatchInvoicesPrinted(batch.id, printed);
      messageApi.success(
        printed
          ? `已批量标记 ${result.updated} 张发票为已打印`
          : `已批量恢复 ${result.updated} 张发票参与导出`,
      );
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "批量更新发票打印状态失败"));
    } finally {
      setBatchInvoicesPrintedUpdating(false);
    }
  };

  const handleItemInvoicesPrintedChange = async (item: VoucherItem, printed: boolean) => {
    if (itemInvoicesPrintedUpdating.has(item.id)) return;
    setItemInvoicesPrintedUpdating((current) => new Set(current).add(item.id));
    try {
      const result = await updateVoucherItemInvoicesPrinted(item.id, printed);
      messageApi.success(
        printed
          ? `已标记本笔 ${result.total} 张发票为已打印`
          : `已恢复本笔 ${result.total} 张发票参与导出`,
      );
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "批量更新本笔发票失败"));
    } finally {
      setItemInvoicesPrintedUpdating((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  };

  const openCandidates = async (item: VoucherItem) => {
    const requestId = candidateRequestRef.current + 1;
    candidateRequestRef.current = requestId;
    setCandidateItem(item);
    setCandidateOpen(true);
    setCandidateSearch("");
    setCandidates([]);
    setCandidateLoading(true);
    try {
      const nextCandidates = await listVoucherMatches(item.id);
      if (candidateRequestRef.current === requestId) {
        setCandidates(nextCandidates);
      }
    } catch (error) {
      if (candidateRequestRef.current === requestId) {
        messageApi.error(getErrorMessage(error, "匹配候选加载失败"));
        setCandidates([]);
      }
    } finally {
      if (candidateRequestRef.current === requestId) {
        setCandidateLoading(false);
      }
    }
  };

  const closeCandidates = () => {
    candidateRequestRef.current += 1;
    setCandidateLoading(false);
    setCandidateOpen(false);
  };

  const handleConfirmCandidate = async (candidate: VoucherMatchCandidate) => {
    if (!candidateItem) return;
    setConfirmingCandidate(candidate.id);
    try {
      const result = await confirmVoucherMatch(candidateItem.id, candidate.id);
      const feishuSync = result.feishu_comment_sync;
      if (feishuSync && (feishuSync.failed || feishuSync.uncertain || feishuSync.processing)) {
        const reason = feishuSync.errors[0]?.reason;
        messageApi.warning(
          `银行回单已确认；飞书评论同步成功 ${feishuSync.synced} 张，` +
          `${feishuSync.failed + feishuSync.uncertain} 张待重试` +
          (feishuSync.processing ? `，${feishuSync.processing} 张同步中` : "") +
          (reason ? `：${reason}` : ""),
        );
      } else {
        messageApi.success(
          "银行回单已人工确认" +
          (feishuSync?.synced ? `，${feishuSync.synced} 张发票已同步飞书评论` : ""),
        );
      }
      closeCandidates();
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "确认匹配失败"));
    } finally {
      setConfirmingCandidate(null);
    }
  };

  const handleUnlinkReceipt = async (item: VoucherItem, attachment: VoucherAttachment) => {
    try {
      await unlinkVoucherDocument(item.id, attachment.id);
      messageApi.success("已从本凭证移除这张银行回单");
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "解除匹配失败"));
    }
  };

  const openReceiptPool = async () => {
    if (!batch) return;
    setReceiptPoolOpen(true);
    setReceiptPoolLoading(true);
    setSelectedReceiptPoolIds(new Set());
    setReceiptPoolSearch("");
    try {
      replaceReceiptPool(await listVoucherReceiptPool(batch.id));
    } catch (error) {
      messageApi.error(getErrorMessage(error, "回单池加载失败"));
      setReceiptPool([]);
    } finally {
      setReceiptPoolLoading(false);
    }
  };

  const handleReceiptInvoiceUploadBatch = async (
    receipt: VoucherReceiptPoolItem,
    files: File[],
  ) => {
    if (!files.length || receiptInvoiceUploadingRef.current.has(receipt.id)) return;
    receiptInvoiceUploadingRef.current.add(receipt.id);
    setReceiptInvoiceUploading((current) => new Set(current).add(receipt.id));
    const uploaded: VoucherAttachment[] = [];
    const failed: string[] = [];
    let duplicates = 0;
    try {
      for (const file of files) {
        try {
          const result = await uploadVoucherReceiptInvoice(receipt.id, file);
          uploaded.push(result.invoice_attachment);
          duplicates += Number(result.deduplicated);
        } catch (error) {
          failed.push(`${file.name}：${getErrorMessage(error, "上传失败")}`);
        }
      }
      setReceiptPool((current) => current.map((row) => {
        if (row.id !== receipt.id) return row;
        const invoices = row.invoice_attachments || [];
        const existingIds = new Set(invoices.map((invoice) => invoice.id));
        return {
          ...row,
          invoice_attachments: [
            ...invoices,
            ...uploaded.filter((invoice) => {
              if (existingIds.has(invoice.id)) return false;
              existingIds.add(invoice.id);
              return true;
            }),
          ],
        };
      }));
      if (uploaded.length) {
        messageApi.success(
          `已放入 ${uploaded.length} 张发票` +
          (duplicates ? `（其中 ${duplicates} 张已存在）` : ""),
        );
      }
      if (failed.length) {
        messageApi.warning(`有 ${failed.length} 张未上传：${failed.slice(0, 2).join("；")}`);
      }
    } finally {
      receiptInvoiceUploadingRef.current.delete(receipt.id);
      setReceiptInvoiceUploading((current) => {
        const next = new Set(current);
        next.delete(receipt.id);
        return next;
      });
    }
  };

  const handleReceiptInvoiceRemove = async (
    receipt: VoucherReceiptPoolItem,
    invoice: VoucherAttachment,
  ) => {
    if (receiptInvoiceRemoving.has(invoice.id)) return;
    setReceiptInvoiceRemoving((current) => new Set(current).add(invoice.id));
    try {
      await removeVoucherReceiptInvoice(receipt.id, invoice.id);
      setReceiptPool((current) => current.map((row) => row.id === receipt.id
        ? {
          ...row,
          invoice_attachments: (row.invoice_attachments || []).filter(
            (attachment) => attachment.id !== invoice.id,
          ),
        }
        : row));
      if (invoice.referenced_from_approval) {
        setSelectedReceiptPoolIds((current) => {
          const next = new Set(current);
          next.delete(receipt.id);
          return next;
        });
      }
      messageApi.success("随附发票已移除");
    } catch (error) {
      messageApi.error(getErrorMessage(error, "移除随附发票失败"));
    } finally {
      setReceiptInvoiceRemoving((current) => {
        const next = new Set(current);
        next.delete(invoice.id);
        return next;
      });
    }
  };

  const openInvoiceReference = async (receipt: VoucherReceiptPoolItem) => {
    const requestId = invoiceReferenceRequestRef.current + 1;
    invoiceReferenceRequestRef.current = requestId;
    setInvoiceReferenceReceipt(receipt);
    setInvoiceReferenceOpen(true);
    setInvoiceReferenceSearch("");
    setInvoiceReferencePage(1);
    setInvoiceReferenceSelectedIds(new Set());
    setInvoiceReferenceLoading(true);
    try {
      const candidates = await listVoucherReceiptInvoiceCandidates(receipt.id);
      if (invoiceReferenceRequestRef.current === requestId) {
        setInvoiceReferenceCandidates(candidates);
      }
    } catch (error) {
      if (invoiceReferenceRequestRef.current === requestId) {
        setInvoiceReferenceCandidates([]);
        messageApi.error(getErrorMessage(error, "审批发票读取失败"));
      }
    } finally {
      if (invoiceReferenceRequestRef.current === requestId) {
        setInvoiceReferenceLoading(false);
      }
    }
  };

  const submitInvoiceReferences = async () => {
    if (!invoiceReferenceReceipt || !invoiceReferenceSelectedIds.size) return;
    setInvoiceReferenceSubmitting(true);
    try {
      const result = await referenceVoucherReceiptInvoices(
        invoiceReferenceReceipt.id,
        Array.from(invoiceReferenceSelectedIds),
      );
      setReceiptPool((current) => current.map((row) => {
        if (row.id !== invoiceReferenceReceipt.id) return row;
        const invoices = row.invoice_attachments || [];
        const existingIds = new Set(invoices.map((invoice) => invoice.id));
        return {
          ...row,
          invoice_attachments: [
            ...invoices,
            ...result.invoice_attachments.filter(
              (invoice) => !existingIds.has(invoice.id),
            ),
          ],
        };
      }));
      messageApi.success(`已引用 ${result.linked || result.invoice_attachments.length} 张审批发票`);
      setSelectedReceiptPoolIds(new Set());
      setReceiptPoolCategory("referenced");
      invoiceReferenceRequestRef.current += 1;
      setInvoiceReferenceOpen(false);
    } catch (error) {
      messageApi.error(getErrorMessage(error, "引用审批发票失败"));
    } finally {
      setInvoiceReferenceSubmitting(false);
    }
  };

  const handleReceiptCategoryChange = async (
    receipt: VoucherReceiptPoolItem,
    category: "pending" | "income" | "other",
  ) => {
    if (receiptCategoryUpdating.has(receipt.id)) return;
    setReceiptCategoryUpdating((current) => new Set(current).add(receipt.id));
    try {
      await updateVoucherReceiptCategory(receipt.id, category);
      setReceiptPool((current) => current.map((row) => (
        row.id === receipt.id ? { ...row, receipt_category: category } : row
      )));
      if (category === "pending") {
        setSelectedReceiptPoolIds((current) => {
          const next = new Set(current);
          next.delete(receipt.id);
          return next;
        });
      }
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "回单分类失败"));
    } finally {
      setReceiptCategoryUpdating((current) => {
        const next = new Set(current);
        next.delete(receipt.id);
        return next;
      });
    }
  };

  const handleReceiptPoolExport = async (
    documentIds?: number[],
    exportLabel = "所选回单",
  ) => {
    const selectedIds = documentIds ?? Array.from(selectedReceiptPoolIds);
    if (
      !batch
      || !selectedIds.length
      || receiptCategoryUpdating.size
      || receiptPoolExporting
    ) return;
    setReceiptPoolExporting(true);
    try {
      const blob = await downloadVoucherReceiptPoolPdf(
        batch.id,
        selectedIds,
      );
      saveBlob(blob, `${batch.year_month}-${exportLabel}.pdf`);
    } catch (error) {
      messageApi.error(await getDownloadErrorMessage(error, "未匹配回单导出失败"));
    } finally {
      setReceiptPoolExporting(false);
    }
  };

  const classifiedReceiptPoolIds = receiptPool
    .filter((row) => (
      !receiptHasApprovalReference(row)
      && (row.receipt_category === "income" || row.receipt_category === "other")
    ))
    .map((row) => row.id);

  const handleDocumentPreview = async (
    attachment: Pick<VoucherAttachment, "id" | "file_name">,
  ) => {
    const previewWindow = window.open("", "_blank");
    if (!previewWindow) {
      messageApi.warning("浏览器阻止了查看窗口，请允许弹出窗口后重试");
      return;
    }
    previewWindow.document.title = attachment.file_name;
    previewWindow.document.body.textContent = "正在加载附件…";
    try {
      const blob = await downloadVoucherDocument(attachment.id);
      const url = URL.createObjectURL(blob);
      previewWindow.location.href = url;
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      previewWindow.close();
      messageApi.error(getErrorMessage(error, "回单查看失败"));
    }
  };

  const handleDeleteReceipt = async (receipt: VoucherReceiptPoolItem) => {
    try {
      await deleteVoucherDocument(receipt.id);
      messageApi.success("未匹配回单已删除");
      setReceiptPool((current) => current.filter((item) => item.id !== receipt.id));
      setSelectedReceiptPoolIds((current) => {
        const next = new Set(current);
        next.delete(receipt.id);
        return next;
      });
      await refreshAll();
    } catch (error) {
      messageApi.error(getErrorMessage(error, "删除回单失败"));
    }
  };

  const openSourceCandidates = async (item: VoucherItem) => {
    setSourceItem(item);
    setSourceOpen(true);
    setSourceLoading(true);
    try {
      const result = await getVoucherSourceAttachments(item.id);
      setSourceCandidates(result.attachments || []);
    } catch (error) {
      messageApi.error(getErrorMessage(error, "读取飞书附件线索失败"));
      setSourceCandidates(item.source_attachment_candidates || []);
    } finally {
      setSourceLoading(false);
    }
  };

  const handleItemExport = async (item: VoucherItem) => {
    if (exporting) return;
    setExporting(`item-${item.id}`);
    try {
      const blob = await downloadVoucherItemPdf(item.id);
      saveBlob(blob, `${item.voucher_no}.pdf`);
    } catch (error) {
      messageApi.error(await getDownloadErrorMessage(error, "单笔凭证生成失败"));
    } finally {
      setExporting(null);
    }
  };

  const handleBatchExport = async (type: "pdf" | "zip") => {
    if (!batch || exporting) return;
    setExporting(type);
    try {
      const blob = type === "pdf"
        ? await downloadVoucherBatchPdf(batch.id)
        : await downloadVoucherBatchZip(batch.id);
      saveBlob(blob, `${batch.year_month}-凭证装订.${type}`);
    } catch (error) {
      messageApi.error(await getDownloadErrorMessage(error, "月度材料生成失败"));
    } finally {
      setExporting(null);
    }
  };

  const columns: ColumnsType<VoucherItem> = [
    {
      title: "凭证号",
      dataIndex: "voucher_no",
      width: 132,
      fixed: "left",
      render: (value: string) => <Text strong copyable>{value}</Text>,
    },
    {
      title: "审批单",
      width: 290,
      render: (_, item) => {
        const expanded = expandedExpenseDetailIds.has(item.id);
        return (
          <div className="voucher-approval-cell">
            <Text strong ellipsis={{ tooltip: item.description || "无事由" }}>
              {item.description || "无事由"}
            </Text>
            {item.source_line_label && <Tag color="blue">{item.source_line_label}</Tag>}
            <Text type="secondary">申请人：{item.applicant_name || "—"}</Text>
            <Text type="secondary">收款方：{item.payee_name || "—"}</Text>
            <Text type="secondary">{item.expense_date || "日期未知"}</Text>
            <Button
              type="link"
              size="small"
              className="voucher-expense-detail-toggle"
              icon={expanded ? <UpOutlined /> : <DownOutlined />}
              aria-expanded={expanded}
              onClick={() => {
                setExpandedExpenseDetailIds((current) => {
                  const next = new Set(current);
                  if (next.has(item.id)) next.delete(item.id);
                  else next.add(item.id);
                  return next;
                });
              }}
            >
              {expanded ? "收起费用明细" : "展开费用明细"}
            </Button>
            {expanded && (
              <div className="voucher-expense-detail-panel">
                <Text strong>完整费用事由</Text>
                <div className="voucher-expense-detail-text">
                  {item.description || "无事由"}
                </div>
                <div className="voucher-expense-detail-meta">
                  <Text type="secondary">金额：{formatMoney(item.amount, item.currency)}</Text>
                  <Text type="secondary">收款方：{item.payee_name || "—"}</Text>
                  <Text type="secondary">费用日期：{item.expense_date || "—"}</Text>
                </div>
                {item.approval_details?.length ? (
                  <div className="voucher-approval-detail-rows">
                    {item.approval_details.map((detail, index) => (
                      <div className="voucher-approval-detail-row" key={`${detail.label}-${index}`}>
                        <Text type="secondary" className="voucher-approval-detail-label">
                          {detail.label}
                        </Text>
                        <div className="voucher-approval-detail-value">{detail.value}</div>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            )}
            {!item.requires_voucher && (
              <Tag color="default">{item.voucher_exclusion_reason || "无需装订"}</Tag>
            )}
            {item.source_attachment_candidate_count > 0 && (
              <Button
                type="link"
                size="small"
                icon={<LinkOutlined />}
                className="voucher-source-link"
                onClick={() => void openSourceCandidates(item)}
              >
                飞书发现 {item.source_attachment_candidate_count} 个附件线索
              </Button>
            )}
          </div>
        );
      },
    },
    {
      title: "金额",
      width: 132,
      align: "right",
      render: (_, item) => <Text strong>{formatMoney(item.amount, item.currency)}</Text>,
    },
    {
      title: "发票（可多张）",
      width: 360,
      render: (_, item) => {
        if (!item.requires_voucher) {
          return <Text type="secondary">无需提供发票</Text>;
        }
        const attachments = invoiceAttachments(item);
        const receiptInvoices = attachments.filter((attachment) => attachment.from_receipt);
        const receiptInvoiceNeedsSync = receiptInvoices.some(
          (attachment) => attachment.feishu_comment_status !== "succeeded",
        );
        const itemUpdating = itemInvoicesPrintedUpdating.has(item.id);
        const allPrinted = attachments.length > 0 && attachments.every((attachment) => attachment.printed);
        return (
          <div className="voucher-material-cell">
            {attachments.length ? attachments.map((attachment) => {
              return (
                <div className="voucher-file-line" key={attachment.id}>
                  <Button
                    type="link"
                    size="small"
                    icon={<DownloadOutlined />}
                    className={attachment.printed ? "voucher-file-printed" : undefined}
                    onClick={() => void handleDocumentDownload(attachment)}
                  >
                    {attachment.file_name}
                  </Button>
                  {attachment.printed && <Tag>已打印</Tag>}
                  {attachment.from_receipt && attachment.feishu_comment_status === "succeeded" && (
                    <Tag color="success">已同步飞书</Tag>
                  )}
                  {attachment.from_receipt && attachment.feishu_comment_status !== "succeeded" && (
                    <Tooltip title={attachment.feishu_comment_error || "可点击下方按钮同步到飞书审批评论"}>
                      <Tag color={attachment.feishu_comment_status === "uncertain"
                        ? "gold"
                        : attachment.feishu_comment_status === "sending"
                          ? "processing"
                          : attachment.feishu_comment_status === "failed"
                            ? "error"
                            : "default"}
                      >
                        {attachment.feishu_comment_status === "uncertain"
                          ? "飞书待核对"
                          : attachment.feishu_comment_status === "sending"
                            ? "飞书同步中"
                            : attachment.feishu_comment_status === "failed"
                              ? "飞书同步失败"
                              : "飞书待同步"}
                      </Tag>
                    </Tooltip>
                  )}
                  {attachment.from_receipt ? (
                    <Tooltip title="这张发票随回单保存；请先解除回单匹配，再到回单池中管理。">
                      <Button
                        type="text"
                        size="small"
                        danger
                        disabled
                        icon={<DeleteOutlined />}
                        aria-label={`随回单保存，不能单独移除 ${attachment.file_name}`}
                      />
                    </Tooltip>
                  ) : (
                    <Popconfirm
                      title="仅从本凭证移除这张发票？"
                      okText="移除"
                      cancelText="取消"
                      onConfirm={() => void handleInvoiceUnlink(item, attachment)}
                    >
                      <Button
                        type="text"
                        size="small"
                        danger
                        disabled={itemUpdating}
                        icon={<DeleteOutlined />}
                        aria-label={`移除 ${attachment.file_name}`}
                      />
                    </Popconfirm>
                  )}
                </div>
              );
            }) : <Text type="secondary">尚未上传发票</Text>}
            {attachments.length > 0 && (
              <Popconfirm
                title={allPrinted ? "恢复本笔全部发票参与导出？" : "把本笔全部发票标记为已打印？"}
                description={allPrinted
                  ? "之后生成的 PDF 和 ZIP 将重新包含本笔全部发票。"
                  : `本笔共 ${attachments.length} 张发票，将一次性全部标记；原文件仍保留。`}
                okText={allPrinted ? "恢复整笔" : "整笔标记已打印"}
                cancelText="取消"
                disabled={batch?.status === "archived" || itemUpdating}
                onConfirm={() => void handleItemInvoicesPrintedChange(item, !allPrinted)}
              >
                <Button
                  type={allPrinted ? "default" : "primary"}
                  size="small"
                  icon={<PrinterOutlined />}
                  loading={itemUpdating}
                  disabled={batch?.status === "archived"}
                >
                  {allPrinted
                    ? `本笔 ${attachments.length} 张已全部打印（点击恢复）`
                    : `本笔全部标记已打印（${attachments.length}张）`}
                </Button>
              </Popconfirm>
            )}
            <Upload
              accept={acceptedFiles}
              showUploadList={false}
              beforeUpload={(file) => {
                void handleInvoiceUpload(item, file);
                return Upload.LIST_IGNORE;
              }}
            >
              <Button size="small" icon={attachments.length ? <PlusOutlined /> : <UploadOutlined />}>
                {attachments.length ? "继续添加发票" : "上传发票"}
              </Button>
            </Upload>
            <Tooltip title="读取审批表单和评论中的 PDF/图片附件，只读取到本系统，不会回写飞书审批。">
              <Button
                size="small"
                icon={<CloudDownloadOutlined />}
                loading={feishuImportingItems.has(item.id)}
                onClick={() => void handleFeishuAttachmentImport(item)}
              >
                {attachments.length ? "从飞书重读附件" : "从飞书读取附件"}
              </Button>
            </Tooltip>
            {receiptInvoices.length > 0 && receiptInvoiceNeedsSync && (
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={feishuCommentSyncingItems.has(item.id)}
                onClick={() => void handleFeishuCommentSync(item)}
              >
                {receiptInvoices.some((attachment) =>
                  attachment.feishu_comment_status === "failed"
                  || attachment.feishu_comment_status === "uncertain")
                  ? "重试飞书评论同步"
                  : "同步到飞书评论"}
              </Button>
            )}
          </div>
        );
      },
    },
    {
      title: "银行回单",
      width: 250,
      render: (_, item) => {
        const attachments = receiptAttachments(item);
        return (
          <div className="voucher-material-cell">
            {attachments.length ? attachments.map((attachment) => (
              <div className="voucher-file-line" key={attachment.id}>
                <Button
                  type="link"
                  size="small"
                  icon={<DownloadOutlined />}
                  onClick={() => void handleDocumentDownload(attachment)}
                >
                  {attachment.file_name}
                </Button>
                <Popconfirm
                  title="仅从本凭证移除这张银行回单？"
                  description={invoiceAttachments(item).some(
                    (invoice) => invoice.from_receipt && invoice.feishu_comment_status === "succeeded",
                  )
                    ? "移除后回单会回到回单池；已发到飞书的评论不会自动删除。"
                    : "移除后回单会回到回单池，可重新匹配。"}
                  okText="移除"
                  cancelText="取消"
                  onConfirm={() => void handleUnlinkReceipt(item, attachment)}
                >
                  <Button type="text" size="small" danger icon={<DeleteOutlined />} aria-label={`移除 ${attachment.file_name}`} />
                </Popconfirm>
              </div>
            )) : (
              <Text type="secondary">
                {item.requires_voucher ? "尚未确认回单" : "没有已关联回单"}
              </Text>
            )}
            {item.requires_voucher ? (
              <Button size="small" icon={attachments.length ? <PlusOutlined /> : <FileSearchOutlined />} onClick={() => void openCandidates(item)}>
                {attachments.length ? "继续添加回单" : "查看匹配候选"}
              </Button>
            ) : (
              <Text type="secondary">无需继续匹配；已有回单仍保留在本审批下</Text>
            )}
          </div>
        );
      },
    },
    {
      title: "完整性",
      width: 132,
      render: (_, item) => {
        const invoices = invoiceAttachments(item);
        if (!item.requires_voucher) return <Tag color="default">无需装订</Tag>;
        if (itemComplete(item)) return <Tag color="success">材料完整</Tag>;
        return (
          <Space direction="vertical" size={2}>
            {!invoices.length && <Tag color="warning">缺发票</Tag>}
            {!receiptAttachments(item).length && <Tag color="error">缺回单</Tag>}
            {item.status === "pending_confirmation" && <Tag color="processing">待确认</Tag>}
          </Space>
        );
      },
    },
    {
      title: "下载打印",
      width: 132,
      fixed: "right",
      render: (_, item) => (
        <Space direction="vertical" size={4}>
          <Tooltip
            title={!item.requires_voucher
              ? "该审批已标记为无需装订"
              : itemComplete(item)
                ? "按审批页 → 待打印发票 → 银行回单生成；已标记打印的发票会跳过"
                : "可先导出审批单和已有材料；缺少的发票或回单不会包含在 PDF 中"}
          >
            <span>
              <Button
                icon={<FilePdfOutlined />}
                disabled={!item.requires_voucher || exporting !== null}
                loading={exporting === `item-${item.id}`}
                onClick={() => void handleItemExport(item)}
              >
                PDF
              </Button>
            </span>
          </Tooltip>
          {item.requires_voucher && (
            <Popconfirm
              title={item.include_approval_in_monthly_pdf !== false
                ? "取消这笔审批单的特殊加入？"
                : "这笔特殊需要加入审批单？"}
              description={item.include_approval_in_monthly_pdf !== false
                ? "恢复默认后，月度 PDF 只导出这笔的发票和回单。"
                : "只对这一笔生效；单笔 PDF 一直包含审批单。"}
              okText={item.include_approval_in_monthly_pdf !== false ? "取消加入" : "特殊加入"}
              cancelText="取消"
              onConfirm={() => void handleMonthlyApprovalChange(item)}
            >
              <Button
                size="small"
                loading={monthlyApprovalUpdating.has(item.id)}
              >
                {item.include_approval_in_monthly_pdf !== false
                  ? "特殊：月度 PDF 含审批单"
                  : "月度 PDF 不含审批单"}
              </Button>
            </Popconfirm>
          )}
          {item.requires_voucher ? (
            <Popconfirm
              title="标记为无需装订？"
              description="这笔不会再计入缺发票、缺回单，也不会影响月度导出。"
              okText="标记"
              cancelText="取消"
              onConfirm={() => void handleRequirementChange(item, false)}
            >
              <Button size="small" icon={<StopOutlined />}>无需装订</Button>
            </Popconfirm>
          ) : (
            <Button size="small" onClick={() => void handleRequirementChange(item, true)}>
              恢复装订
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const receiptColumns: ColumnsType<ReceiptUploadRow> = [
    {
      title: "回单文件",
      width: 220,
      render: (_, row) => (
        <div>
          <Text ellipsis={{ tooltip: row.file.name }}>{row.file.name}</Text>
          <br />
          <Text type="secondary">{formatSize(row.file.size)}</Text>
        </div>
      ),
    },
    {
      title: "金额（可选）",
      width: 150,
      render: (_, row) => (
        <InputNumber
          min={0}
          precision={2}
          controls={false}
          placeholder="自动识别或补录"
          value={row.amount}
          onChange={(value) => updateReceiptRow(row.key, { amount: value ?? undefined })}
          style={{ width: "100%" }}
        />
      ),
    },
    {
      title: "收款方（可选）",
      width: 190,
      render: (_, row) => (
        <Input
          placeholder="自动识别或补录"
          value={row.payee_name}
          onChange={(event) => updateReceiptRow(row.key, { payee_name: event.target.value })}
        />
      ),
    },
    {
      title: "交易日期（可选）",
      width: 160,
      render: (_, row) => (
        <DatePicker
          value={row.transaction_date ? dayjs(row.transaction_date) : null}
          onChange={(value) => updateReceiptRow(row.key, { transaction_date: value?.format("YYYY-MM-DD") || "" })}
          style={{ width: "100%" }}
        />
      ),
    },
    {
      title: "",
      width: 54,
      render: (_, row) => (
        <Button
          type="text"
          danger
          icon={<DeleteOutlined />}
          aria-label={`移除 ${row.file.name}`}
          onClick={() => setReceiptRows((current) => current.filter((item) => item.key !== row.key))}
        />
      ),
    },
  ];

  return (
    <div className="voucher-page">
      {messageContext}
      {modalContext}
      <div className="voucher-page-heading">
        <div>
          <Title level={3}>凭证装订</Title>
          <Text type="secondary">读取费用看板中 2026 年 7 月起的已通过审批单，按月整理全部发票和银行回单</Text>
        </div>
        <Space wrap>
          <Button
            icon={<WarningOutlined />}
            danger={incompleteData.total > 0}
            onClick={openIncomplete}
          >
            累计不完整{incompleteData.total ? `（${incompleteData.total}）` : ""}
          </Button>
          <DatePicker
            picker="month"
            format="YYYY-MM"
            allowClear={false}
            minDate={VOUCHER_START_MONTH}
            value={month}
            disabled={receiptClearing}
            onChange={(value) => {
              if (!value) return;
              clearIncompleteJump();
              setMonth(value.startOf("month"));
            }}
          />
          <Button
            type="primary"
            icon={batch ? <ReloadOutlined /> : <PlusOutlined />}
            loading={syncing}
            disabled={receiptClearing}
            onClick={() => void handleCreateOrSync()}
          >
            刷新本月本地凭证
          </Button>
        </Space>
      </div>

      <Alert
        showIcon
        type="info"
        className="voucher-module-alert"
        title="独立凭证装订模块：不会改变费用看板原有审批、分类、预算和同步规则。"
        description="当前为本地空数据版：不读取飞书、不发送评论。仅使用本地新建的数据；如需演示，请仅上传自行制作的虚构样例。"
      />

      <Spin spinning={batchLoading}>
        {!batch ? (
          <Card className="voucher-empty-card">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={`${yearMonth} 尚未载入，点击下方读取费用看板中本月已通过审批单`}
            >
              <Button type="primary" loading={syncing} onClick={() => void handleCreateOrSync()}>
                载入本月已通过审批单
              </Button>
            </Empty>
          </Card>
        ) : (
          <>
            <Row gutter={[16, 16]} className="voucher-summary-row">
              <Col flex="1 1 160px"><Card><Statistic title="总笔数" value={batch.summary.total} suffix="笔" /></Card></Col>
              <Col flex="1 1 160px"><Card className="voucher-stat-complete"><Statistic title="材料完整" value={batch.summary.complete} suffix="笔" /></Card></Col>
              <Col flex="1 1 160px"><Card><Statistic title="缺发票" value={batch.summary.missing_invoice} suffix="笔" styles={{ content: { color: "#d97706" } }} /></Card></Col>
              <Col flex="1 1 160px"><Card><Statistic title="缺回单" value={batch.summary.missing_receipt} suffix="笔" styles={{ content: { color: "#dc2626" } }} /></Card></Col>
              <Col flex="1 1 160px"><Card><Statistic title="待人工确认" value={batch.summary.pending_confirmation} suffix="笔" styles={{ content: { color: "#1677ff" } }} /></Card></Col>
              <Col flex="1 1 160px"><Card><Statistic title="无需装订" value={batch.summary.no_voucher_required} suffix="笔" /></Card></Col>
            </Row>

            <Card className="voucher-workbench-card">
              <div className="voucher-toolbar">
                <Space wrap>
                  <input
                    ref={receiptInputRef}
                    hidden
                    multiple
                    type="file"
                    accept={acceptedFiles}
                    onChange={(event) => handleReceiptSelection(event.target.files)}
                  />
                  <Button
                    type="primary"
                    icon={<UploadOutlined />}
                    disabled={receiptClearing}
                    onClick={() => receiptInputRef.current?.click()}
                  >
                    批量上传银行回单
                  </Button>
                  <Button
                    icon={<FileSearchOutlined />}
                    loading={receiptAutoMatching}
                    disabled={receiptClearing}
                    onClick={() => void handleReceiptAutoMatch()}
                  >
                    自动匹配回单
                  </Button>
                  <Button
                    icon={<InboxOutlined />}
                    disabled={receiptClearing}
                    onClick={() => void openReceiptPool()}
                  >
                    回单池{batch.summary.unmatched_receipts ? `（${batch.summary.unmatched_receipts} 未匹配）` : ""}
                  </Button>
                  <Button
                    danger
                    icon={<DeleteOutlined />}
                    disabled={
                      !batch.summary.receipt_total
                      || batch.status === "archived"
                      || receiptUploading
                      || receiptAutoMatching
                      || receiptClearing
                    }
                    loading={receiptClearing}
                    onClick={handleClearReceipts}
                  >
                    清空本月回单
                  </Button>
                  <Popconfirm
                    title="把本月全部发票标记为已打印？"
                    description="原文件仍保留并可下载；之后生成的单笔 PDF、月度 PDF 和 ZIP 将跳过这些发票。"
                    okText="全部标记已打印"
                    cancelText="取消"
                    disabled={batch.status === "archived" || batchInvoicesPrintedUpdating}
                    onConfirm={() => void handleBatchInvoicesPrintedChange(true)}
                  >
                    <Button
                      icon={<PrinterOutlined />}
                      loading={batchInvoicesPrintedUpdating}
                      disabled={batch.status === "archived" || receiptClearing}
                    >
                      批量标记发票已打印
                    </Button>
                  </Popconfirm>
                  <Popconfirm
                    title="恢复本月全部发票参与导出？"
                    description="之后生成的单笔 PDF、月度 PDF 和 ZIP 将重新包含全部发票。"
                    okText="全部恢复"
                    cancelText="取消"
                    disabled={batch.status === "archived" || batchInvoicesPrintedUpdating}
                    onConfirm={() => void handleBatchInvoicesPrintedChange(false)}
                  >
                    <Button
                      type="link"
                      disabled={batch.status === "archived" || batchInvoicesPrintedUpdating || receiptClearing}
                    >
                      恢复全部发票
                    </Button>
                  </Popconfirm>
                </Space>
                <Space wrap>
                  <Tooltip
                    title={hasVoucherItems
                      ? "按凭证号顺序导出；已标记打印的审批页或发票会跳过"
                      : "本月暂无需要装订的凭证"}
                  >
                    <span>
                      <Button
                        type="primary"
                        icon={<FilePdfOutlined />}
                        disabled={
                          !hasVoucherItems
                          || exporting !== null
                          || receiptClearing
                        }
                        loading={exporting === "pdf"}
                        onClick={() => void handleBatchExport("pdf")}
                      >
                        导出本月全部
                      </Button>
                    </span>
                  </Tooltip>
                  <Tooltip title={hasVoucherItems ? "下载逐笔凭证 ZIP；缺少或已标记打印的发票不会包含" : "本月暂无需要装订的凭证"}>
                    <span>
                      <Button
                        icon={<DownloadOutlined />}
                        disabled={
                          !hasVoucherItems
                          || exporting !== null
                          || receiptClearing
                        }
                        loading={exporting === "zip"}
                        onClick={() => void handleBatchExport("zip")}
                      >
                        月度 ZIP
                      </Button>
                    </span>
                  </Tooltip>
                </Space>
              </div>

              <div className="voucher-filterbar">
                <Select
                  value={statusFilter}
                  style={{ width: 170 }}
                  options={[
                    { value: "all", label: "全部完整性" },
                    { value: "complete", label: "材料完整" },
                    { value: "missing_invoice", label: "缺发票" },
                    { value: "missing_receipt", label: "缺回单" },
                    { value: "pending_confirmation", label: "待人工确认" },
                    { value: "no_voucher_required", label: "无需装订" },
                  ]}
                  onChange={(value) => { setStatusFilter(value); setPage(1); }}
                />
                <Input.Search
                  allowClear
                  value={searchText}
                  placeholder="搜索凭证号、申请人、收款方或事由"
                  style={{ width: 360, maxWidth: "100%" }}
                  onChange={(event) => setSearchText(event.target.value)}
                  onSearch={(value) => { setKeyword(value.trim()); setPage(1); }}
                />
                <Button icon={<ReloadOutlined />} onClick={() => void refreshAll()}>刷新</Button>
              </div>

              <Table<VoucherItem>
                rowKey="id"
                columns={columns}
                dataSource={items}
                loading={tableLoading}
                pagination={false}
                scroll={{ x: 1500 }}
                onRow={(item) => ({
                  id: `voucher-item-${item.id}`,
                  className: highlightedVoucherItemId === item.id ? "voucher-jump-highlight" : "",
                })}
                locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的凭证" /> }}
              />
              <Flex justify="space-between" align="center" wrap className="voucher-pagination">
                <Text type="secondary">共 {total} 笔</Text>
                <Pagination current={page} pageSize={PAGE_SIZE} total={total} showSizeChanger={false} onChange={setPage} />
              </Flex>
            </Card>
          </>
        )}
      </Spin>

      <Modal
        title="批量上传银行回单"
        width={900}
        open={receiptUploadOpen}
        okText="上传并生成匹配候选"
        cancelText="取消"
        okButtonProps={{ disabled: !receiptRows.length, loading: receiptUploading }}
        onOk={() => void submitReceiptUpload()}
        onCancel={() => !receiptUploading && setReceiptUploadOpen(false)}
      >
        <Alert
          showIcon
          type="warning"
          title="系统按金额（70 分）、收款方（20 分）、日期（10 分）匹配；只有唯一高可信候选会自动确认。"
          description="其余回单保留在待匹配区供人工确认；批量 PDF 会逐页识别，不会把一个手工名称或金额复制到所有页。支持 PDF、JPG、PNG。"
          style={{ marginBottom: 16 }}
        />
        <Table<ReceiptUploadRow>
          size="small"
          rowKey="key"
          columns={receiptColumns}
          dataSource={receiptRows}
          pagination={false}
          scroll={{ x: 760, y: 360 }}
        />
        <Button
          type="dashed"
          block
          icon={<PlusOutlined />}
          style={{ marginTop: 12 }}
          onClick={() => receiptInputRef.current?.click()}
        >
          重新选择一组文件
        </Button>
      </Modal>

      <Drawer
        title={candidateItem ? `${candidateItem.voucher_no} · 银行回单匹配候选` : "银行回单匹配候选"}
        size={620}
        open={candidateOpen}
        onClose={closeCandidates}
      >
        <Alert
          showIcon
          type="info"
          title="高可信候选会由“自动匹配回单”直接绑定；这里保留的是需要你复核的候选。"
          style={{ marginBottom: 16 }}
        />
        <Input.Search
          allowClear
          value={candidateSearch}
          placeholder="搜索文件名、金额、收款方、日期或银行流水号"
          onChange={(event) => setCandidateSearch(event.target.value)}
          style={{ marginBottom: 16 }}
        />
        <Spin spinning={candidateLoading}>
          <List
            dataSource={filteredCandidates}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={candidates.length
                    ? "没有符合搜索条件的回单"
                    : "回单池中暂无匹配候选"}
                />
              ),
            }}
            renderItem={(candidate) => {
              const score = normalizeScore(candidate.score);
              const receiptName = candidateReceiptName(candidate);
              const receiptAttachment: VoucherAttachment = typeof candidate.receipt === "string"
                ? {
                    id: candidate.id,
                    file_name: candidate.receipt,
                    content_type: "application/octet-stream",
                    size: 0,
                    uploaded_at: "",
                  }
                : candidate.receipt;
              return (
                <List.Item>
                  <Card className="voucher-candidate-card" size="small">
                    <Flex gap={16} align="center">
                      <Progress type="circle" percent={score} size={72} strokeColor={score >= 90 ? "#16a34a" : score >= 70 ? "#1677ff" : "#d97706"} />
                      <div className="voucher-candidate-main">
                        <Flex justify="space-between" gap={12} align="start">
                          <div>
                            <Text strong>{receiptName}</Text>
                            <div><Text type="secondary">{candidate.reason || "系统按三个维度计算候选分数"}</Text></div>
                          </div>
                          <Button type="link" icon={<DownloadOutlined />} onClick={() => void handleDocumentDownload(receiptAttachment)}>下载查看</Button>
                        </Flex>
                        <Descriptions size="small" column={2} className="voucher-candidate-descriptions">
                          <Descriptions.Item label="金额">{candidate.amount == null ? "未识别" : formatMoney(candidate.amount)}</Descriptions.Item>
                          <Descriptions.Item label="收款方">{candidate.payee_name || "未识别"}</Descriptions.Item>
                          <Descriptions.Item label="日期">{candidate.transaction_date || "未识别"}</Descriptions.Item>
                          <Descriptions.Item label="银行流水号">{candidate.bank_reference || "未识别"}</Descriptions.Item>
                        </Descriptions>
                        <div className="voucher-score-breakdown">
                          <Tag>金额 {breakdownScore(candidate.breakdown?.amount)}/70</Tag>
                          <Tag>收款方 {breakdownScore(candidate.breakdown?.payee)}/20</Tag>
                          <Tag>日期 {breakdownScore(candidate.breakdown?.date)}/10</Tag>
                        </div>
                        <Popconfirm
                          title="确认匹配此回单？"
                          description="如果回单附带了本地上传的发票，会同时发送到这笔飞书审批评论；已发送的评论不能由本系统自动撤回。"
                          okText="确认本地匹配"
                          cancelText="取消"
                          onConfirm={() => void handleConfirmCandidate(candidate)}
                        >
                          <Button
                            type="primary"
                            block
                            loading={confirmingCandidate === candidate.id}
                          >
                            人工确认此回单
                          </Button>
                        </Popconfirm>
                      </div>
                    </Flex>
                  </Card>
                </List.Item>
              );
            }}
          />
        </Spin>
      </Drawer>

      <Drawer
        title="累计不完整审批"
        size={760}
        open={incompleteOpen}
        onClose={() => setIncompleteOpen(false)}
        extra={(
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={incompleteLoading}
            onClick={() => void loadIncomplete()}
          >
            刷新
          </Button>
        )}
      >
        <Alert
          showIcon
          type="warning"
          title="这里累计显示从 2026-07 起，至今仍缺回单或发票的审批。"
          description="可以跨月追溯；资料补齐后，刷新即会自动从清单移除。已归档月份也会保留显示，但只能查看。"
          style={{ marginBottom: 16 }}
        />
        <Row gutter={12} style={{ marginBottom: 16 }}>
          <Col span={8}>
            <Card size="small"><Statistic title="不完整审批" value={incompleteData.total} suffix="笔" /></Card>
          </Col>
          <Col span={8}>
            <Card size="small"><Statistic title="缺发票" value={incompleteData.missing_invoice} suffix="笔" /></Card>
          </Col>
          <Col span={8}>
            <Card size="small"><Statistic title="缺回单" value={incompleteData.missing_receipt} suffix="笔" /></Card>
          </Col>
        </Row>
        <Spin spinning={incompleteLoading}>
          {!incompleteData.months.length ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="所有月份的装订资料都已完整" />
          ) : (
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              {incompleteData.months.map((monthGroup) => (
                <Card
                  key={monthGroup.year_month}
                  size="small"
                  title={(
                    <Space wrap>
                      <Text strong>{monthGroup.year_month}</Text>
                      <Tag color="red">不完整 {monthGroup.count}</Tag>
                      <Tag>缺发票 {monthGroup.missing_invoice}</Tag>
                      <Tag>缺回单 {monthGroup.missing_receipt}</Tag>
                      {monthGroup.batch_status === "archived" && <Tag color="default">已归档</Tag>}
                    </Space>
                  )}
                  extra={(
                    <Button size="small" onClick={() => openIncompleteMonth(monthGroup.year_month)}>
                      查看该月
                    </Button>
                  )}
                >
                  <List
                    size="small"
                    dataSource={monthGroup.items}
                    renderItem={(item) => (
                      <List.Item
                        actions={[
                          <Button
                            key="complete"
                            type="link"
                            size="small"
                            onClick={() => openIncompleteItem(monthGroup.year_month, item)}
                          >
                            去完善
                          </Button>,
                        ]}
                      >
                        <List.Item.Meta
                          title={(
                            <Space wrap>
                              <Text strong>{item.voucher_no}</Text>
                              <Text>{item.definition_name || item.description || "费用审批"}</Text>
                              {item.missing_invoice && <Tag color="orange">缺发票</Tag>}
                              {item.missing_receipt && <Tag color="red">缺回单</Tag>}
                            </Space>
                          )}
                          description={(
                            <Space wrap split={<Text type="secondary">·</Text>}>
                              <Text type="secondary">申请人：{item.applicant_name || "—"}</Text>
                              <Text type="secondary">收款方：{item.payee_name || "—"}</Text>
                              {item.amount != null && (
                                <Text type="secondary">金额：{formatMoney(item.amount, item.currency)}</Text>
                              )}
                              {item.date && <Text type="secondary">日期：{item.date}</Text>}
                            </Space>
                          )}
                        />
                      </List.Item>
                    )}
                  />
                </Card>
              ))}
            </Space>
          )}
        </Spin>
      </Drawer>

      <Drawer title={`${yearMonth} 银行回单池`} size={760} open={receiptPoolOpen} onClose={() => setReceiptPoolOpen(false)}>
        <Alert
          showIcon
          type="info"
          title="这里只显示尚未匹配的回单；已匹配回单不会重复出现。"
          description="含审批引用发票的回单会单独归入“已引用”，不会在其他分类重复显示。导出会保持列表顺序，并让每笔发票在前、对应回单在后。"
          style={{ marginBottom: 16 }}
        />
        <Input.Search
          allowClear
          value={receiptPoolSearch}
          placeholder="搜索回单/发票文件名、金额、对方名称、日期或银行流水号"
          onChange={(event) => {
            setReceiptPoolSearch(event.target.value);
            setSelectedReceiptPoolIds(new Set());
          }}
          style={{ marginBottom: 12 }}
        />
        <Flex justify="space-between" align="center" gap={12} wrap style={{ marginBottom: 12 }}>
          <Segmented
            value={receiptPoolCategory}
            options={[
              {
                value: "pending",
                label: `待匹配及其他 ${receiptPool.filter((row) => receiptMatchesPoolCategory(row, "pending")).length}`,
              },
              {
                value: "income",
                label: `收入 ${receiptPool.filter((row) => receiptMatchesPoolCategory(row, "income")).length}`,
              },
              {
                value: "referenced",
                label: `已引用 ${receiptPool.filter((row) => receiptMatchesPoolCategory(row, "referenced")).length}`,
              },
            ]}
            onChange={(value) => {
              setReceiptPoolCategory(value as ReceiptPoolCategory);
              setSelectedReceiptPoolIds(new Set());
            }}
          />
          <Space wrap>
            <Button
              size="small"
              disabled={
                !classifiedReceiptPoolIds.length
                || receiptPoolLoading
                || receiptCategoryUpdating.size > 0
                || receiptPoolExporting
                || receiptClearing
              }
              loading={receiptPoolExporting}
              onClick={() => void handleReceiptPoolExport(classifiedReceiptPoolIds, "全部收入及其他回单")}
            >
              导出全部收入及其他（{classifiedReceiptPoolIds.length}）
            </Button>
            <Button
              type="primary"
              icon={<FilePdfOutlined />}
              disabled={
                !selectedReceiptPoolIds.size
                || receiptCategoryUpdating.size > 0
                || receiptClearing
              }
              loading={receiptPoolExporting}
              onClick={() => void handleReceiptPoolExport()}
            >
              导出所选（{selectedReceiptPoolIds.size}）
            </Button>
          </Space>
        </Flex>
        <Spin spinning={receiptPoolLoading || receiptClearing}>
          {!receiptPool.length ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有未匹配回单" />
          ) : ([receiptPoolCategory] as const).map((category) => {
            const categoryRows = receiptPool.filter(
              (receipt) => receiptMatchesPoolCategory(receipt, category),
            );
            const rows = filteredReceiptPool.filter(
              (receipt) => receiptMatchesPoolCategory(receipt, category),
            );
            const title = receiptPoolCategoryTitle(category);
            const categoryIds = (receiptPoolSearch.trim() ? rows : categoryRows)
              .map((receipt) => receipt.id);
            return (
              <div className="voucher-receipt-section" key={category}>
                <Flex justify="space-between" align="center" gap={12}>
                  <Title level={5}>
                    {title}（{receiptPoolSearch.trim()
                      ? `显示 ${rows.length} / 共 ${categoryRows.length}`
                      : rows.length}）
                  </Title>
                  <Button
                    size="small"
                    icon={<FilePdfOutlined />}
                    disabled={
                      !categoryIds.length
                      || receiptPoolLoading
                      || receiptCategoryUpdating.size > 0
                      || receiptPoolExporting
                      || receiptClearing
                    }
                    onClick={() => void handleReceiptPoolExport(
                      categoryIds,
                      category === "pending"
                        ? receiptPoolSearch.trim() ? "无票支出-搜索结果" : "无票支出"
                        : receiptPoolSearch.trim() ? `${title}搜索结果` : `${title}回单`,
                    )}
                  >
                    {receiptPoolSearch.trim()
                      ? `导出当前结果（${rows.length}）`
                      : `导出全部${title}`}
                  </Button>
                </Flex>
                <List
                  dataSource={rows}
                  locale={{
                    emptyText: (
                      <Empty
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                        description={receiptPoolSearch.trim()
                          ? "没有符合搜索条件的回单"
                          : `暂无${title}回单`}
                      />
                    ),
                  }}
                  renderItem={(receipt) => (
                    <List.Item
                      actions={[
                        <Button key="preview" type="link" icon={<FileSearchOutlined />} onClick={() => void handleDocumentPreview(receipt)}>查看</Button>,
                        <Button key="export" type="link" icon={<FilePdfOutlined />} onClick={() => void handleReceiptPoolExport([receipt.id], receipt.file_name.replace(/\.pdf$/i, ""))}>单独导出</Button>,
                        <Button key="download" type="link" icon={<DownloadOutlined />} onClick={() => void handleDocumentDownload(receipt)}>下载</Button>,
                        <Popconfirm
                          key="delete"
                          title={(receipt.invoice_attachments || []).length
                            ? "永久删除这份回单及其随附发票？"
                            : "永久删除这份未匹配回单？"}
                          okText="删除"
                          cancelText="取消"
                          onConfirm={() => void handleDeleteReceipt(receipt)}
                        >
                          <Button type="link" danger disabled={receiptClearing}>删除</Button>
                        </Popconfirm>,
                      ]}
                    >
                      <List.Item.Meta
                        avatar={(
                          <Checkbox
                            disabled={receiptCategoryUpdating.has(receipt.id)}
                            checked={selectedReceiptPoolIds.has(receipt.id)}
                            onChange={(event) => setSelectedReceiptPoolIds((current) => {
                              const next = new Set(current);
                              if (event.target.checked) next.add(receipt.id);
                              else next.delete(receipt.id);
                              return next;
                            })}
                          />
                        )}
                        title={<Text strong>{receipt.file_name}</Text>}
                        description={
                          <Space direction="vertical" size={6}>
                            <Space wrap split={<Text type="secondary">·</Text>}>
                              <Text type="secondary">金额：{receipt.amount == null ? "未识别" : formatMoney(receipt.amount)}</Text>
                              <Text type="secondary">对方名称：{receipt.payee_name || "未识别"}</Text>
                              <Text type="secondary">日期：{receipt.transaction_date || "未识别"}</Text>
                              {receipt.bank_reference && <Text type="secondary">流水号：{receipt.bank_reference}</Text>}
                              <Text type="secondary">{formatSize(receipt.size)}</Text>
                            </Space>
                            <Space wrap>
                              <Text type="secondary">归类：</Text>
                              <Select
                                size="small"
                                value={receipt.receipt_category || "pending"}
                                loading={receiptCategoryUpdating.has(receipt.id)}
                                disabled={
                                  receiptCategoryUpdating.has(receipt.id)
                                  || receiptClearing
                                }
                                style={{ width: 120 }}
                                options={[
                                  { value: "pending", label: "待匹配" },
                                  { value: "income", label: "收入" },
                                  { value: "other", label: "其他" },
                                ]}
                                onChange={(value) => void handleReceiptCategoryChange(receipt, value)}
                              />
                              {receipt.receipt_direction === "income" && <Tag color="green">自动识别为入账</Tag>}
                            </Space>
                            <Space wrap align="center">
                              <Text type="secondary">随附发票（可选）：</Text>
                              {(receipt.invoice_attachments || []).map((invoice) => (
                                <Space key={invoice.id} size={2}>
                                  <Space.Compact>
                                    <Button
                                      size="small"
                                      type="link"
                                      icon={<FilePdfOutlined />}
                                      onClick={() => void handleDocumentPreview(invoice)}
                                    >
                                      {invoice.file_name}
                                    </Button>
                                    <Button
                                      size="small"
                                      type="text"
                                      icon={<DownloadOutlined />}
                                      aria-label={`下载 ${invoice.file_name}`}
                                      onClick={() => void handleDocumentDownload(invoice)}
                                    />
                                    <Popconfirm
                                      title={invoice.referenced_from_approval
                                        ? "仅移除引用？原审批中的发票不会删除。"
                                        : "移除并删除这份随附发票？"}
                                      okText="移除"
                                      cancelText="取消"
                                      onConfirm={() => void handleReceiptInvoiceRemove(receipt, invoice)}
                                    >
                                      <Button
                                        size="small"
                                        type="text"
                                        danger
                                        icon={<DeleteOutlined />}
                                        loading={receiptInvoiceRemoving.has(invoice.id)}
                                        aria-label={`移除 ${invoice.file_name}`}
                                      />
                                    </Popconfirm>
                                  </Space.Compact>
                                  {invoice.referenced_from_approval && <Tag color="blue">审批引用</Tag>}
                                </Space>
                              ))}
                              <Upload
                                accept={acceptedFiles}
                                multiple
                                showUploadList={false}
                                disabled={
                                  receipt.can_upload_invoice === false
                                  || receiptClearing
                                  || receiptInvoiceUploading.has(receipt.id)
                                }
                                beforeUpload={(file, fileList) => {
                                  if (file.uid === fileList[0]?.uid) {
                                    void handleReceiptInvoiceUploadBatch(receipt, fileList);
                                  }
                                  return Upload.LIST_IGNORE;
                                }}
                              >
                                <Button
                                  size="small"
                                  icon={<UploadOutlined />}
                                  loading={receiptInvoiceUploading.has(receipt.id)}
                                >
                                  上传发票（可多选）
                                </Button>
                              </Upload>
                              <Button
                                size="small"
                                icon={<LinkOutlined />}
                                disabled={
                                  receipt.can_upload_invoice === false
                                  || receiptClearing
                                }
                                onClick={() => void openInvoiceReference(receipt)}
                              >
                                引用审批发票
                              </Button>
                            </Space>
                            <Text type="secondary">
                              本地空数据版：发票仅保存在本地，不会同步飞书评论。
                            </Text>
                          </Space>
                        }
                      />
                    </List.Item>
                  )}
                />
              </div>
            );
          })}
        </Spin>
      </Drawer>

      <Modal
        title={invoiceReferenceReceipt
          ? `为回单引用审批发票 · ${invoiceReferenceReceipt.file_name}`
          : "引用审批发票"}
        width={900}
        open={invoiceReferenceOpen}
        okText={`确认引用（${invoiceReferenceSelectedIds.size}）`}
        cancelText="取消"
        okButtonProps={{ disabled: !invoiceReferenceSelectedIds.size }}
        confirmLoading={invoiceReferenceSubmitting}
        onOk={() => void submitInvoiceReferences()}
        onCancel={() => {
          if (!invoiceReferenceSubmitting) {
            invoiceReferenceRequestRef.current += 1;
            setInvoiceReferenceOpen(false);
            setInvoiceReferenceLoading(false);
          }
        }}
      >
        <Alert
          showIcon
          type="info"
          title="这里只建立引用，不会重复上传，也不会移动或删除原审批中的发票。"
          description="可一次选择多张；列表包含本回单月份及以前已进入凭证装订的审批发票。"
          style={{ marginBottom: 12 }}
        />
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <Input.Search
            allowClear
            value={invoiceReferenceSearch}
            placeholder="搜索月份、凭证号、申请人、收款方、金额或发票文件名"
            onChange={(event) => {
              setInvoiceReferenceSearch(event.target.value);
              setInvoiceReferencePage(1);
            }}
            style={{ flex: 1 }}
          />
          <Button
            disabled={!filteredInvoiceReferenceCandidates.length}
            onClick={() => setInvoiceReferenceSelectedIds((current) => {
              const next = new Set(current);
              filteredInvoiceReferenceCandidates.forEach((candidate) => {
                next.add(candidate.document_id);
              });
              return next;
            })}
          >
            全选搜索结果（{filteredInvoiceReferenceCandidates.length}）
          </Button>
          <Button
            disabled={!invoiceReferenceSelectedIds.size}
            onClick={() => setInvoiceReferenceSelectedIds(new Set())}
          >
            清空已选
          </Button>
        </div>
        <Table<VoucherReceiptInvoiceCandidate>
          size="small"
          rowKey="document_id"
          loading={invoiceReferenceLoading}
          dataSource={filteredInvoiceReferenceCandidates}
          rowSelection={{
            selectedRowKeys: Array.from(invoiceReferenceSelectedIds),
            preserveSelectedRowKeys: true,
            onChange: (keys) => setInvoiceReferenceSelectedIds(
              new Set(keys.map((key) => Number(key))),
            ),
          }}
          scroll={{ y: 420 }}
          pagination={{
            current: invoiceReferencePage,
            pageSize: 20,
            showSizeChanger: false,
            showTotal: (total) => `共 ${total} 张`,
            onChange: (page) => setInvoiceReferencePage(page),
          }}
          locale={{ emptyText: "没有可引用的审批发票" }}
          columns={[
            {
              title: "月份 / 凭证号",
              width: 150,
              render: (_, candidate) => (
                <Space direction="vertical" size={0}>
                  <Text>{candidate.year_month}</Text>
                  <Text type="secondary">{candidate.voucher_no}</Text>
                </Space>
              ),
            },
            {
              title: "审批",
              render: (_, candidate) => (
                <Space direction="vertical" size={0}>
                  <Text>{candidate.definition_name || candidate.description || "费用审批"}</Text>
                  <Text type="secondary">
                    {candidate.applicant_name || "—"} · {candidate.payee_name || "—"}
                  </Text>
                </Space>
              ),
            },
            {
              title: "金额",
              width: 120,
              render: (_, candidate) => candidate.amount == null
                ? "—"
                : formatMoney(candidate.amount, candidate.currency),
            },
            {
              title: "发票文件",
              width: 230,
              render: (_, candidate) => (
                <Button
                  type="link"
                  size="small"
                  icon={<FileSearchOutlined />}
                  onClick={() => void handleDocumentPreview(candidate.invoice_attachment)}
                >
                  {candidate.invoice_attachment.file_name}
                </Button>
              ),
            },
          ]}
        />
      </Modal>

      <Modal
        title={sourceItem ? `${sourceItem.voucher_no} · 飞书附件线索` : "飞书附件线索"}
        open={sourceOpen}
        footer={<Button onClick={() => setSourceOpen(false)}>关闭</Button>}
        onCancel={() => setSourceOpen(false)}
      >
        <Alert
          showIcon
          type="warning"
          title="这里显示审批单原始数据中识别到的附件线索，文件不一定已经导入。"
          description="本地空数据版已停用飞书附件同步，请仅使用自行制作的虚构演示附件。"
          style={{ marginBottom: 16 }}
        />
        <Spin spinning={sourceLoading}>
          <List
            bordered
            dataSource={sourceCandidates}
            locale={{ emptyText: "没有可显示的附件线索" }}
            renderItem={(item) => (
              <List.Item>
                <List.Item.Meta
                  avatar={<FileAddOutlined />}
                  title={item.file_name || item.name || "未命名附件"}
                  description={item.type || (item.file_token ? `文件标识：${item.file_token}` : "仅元信息")}
                />
              </List.Item>
            )}
          />
        </Spin>
      </Modal>
    </div>
  );
}
