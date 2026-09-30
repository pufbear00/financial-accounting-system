export interface Category {
  id: number;
  name: string;
  description: string;
  sort_order: number;
  created_at: string;
}

export interface Approval {
  id: number;
  instance_code: string;
  task_id: string | null;
  definition_code: string;
  definition_name: string | null;
  applicant_id: string | null;
  applicant_name: string | null;
  status: number | null;
  amount: number | null;
  currency: string | null;
  exchange_rate: number | null;
  amount_cny: number | null;
  category_name: string | null;
  category_id: number | null;
  expense_date: string | null;
  submit_date: string | null;
  description: string | null;
  topic: number | null;
  synced_at: string | null;
}

export interface ApprovalListResponse {
  items: Approval[];
  total: number;
  page: number;
  page_size: number;
  sum_amount: number;
}

export interface Budget {
  id: number;
  year_month: string;
  category_id: number;
  amount: number;
  note: string;
  created_at: string;
  updated_at: string;
}

export interface BudgetWithSpent {
  id: number;
  year_month: string;
  category_id: number;
  category_name: string;
  amount: number;
  spent: number;
  usage_pct: number;
  note: string;
}

export interface CategorySummary {
  category_id: number | null;
  category_name: string;
  amount: number;
  count: number;
  budget: number | null;
  usage_pct: number | null;
}

export interface SourceBreakdown {
  feishu_amount: number;
  feishu_count: number;
  airwallex_amount: number;
  airwallex_count: number;
}

export interface MonthlySummary {
  year_month: string;
  total_amount: number;
  approved_count: number;
  pending_count: number;
  total_budget: number;
  budget_usage_pct: number;
  by_category: CategorySummary[];
  by_source: SourceBreakdown;
}

export interface TrendPoint {
  year_month: string;
  total: number;
}

export interface BudgetAlert {
  category_id: number;
  category_name: string;
  budget: number;
  spent: number;
  usage_pct: number;
}

export interface UserInfo {
  id: number;
  username: string;
  display_name: string;
  role: string;
  category_ids: number[];
  feishu_open_id: string | null;
}

export interface SyncLog {
  id: number;
  sync_type: string;
  total_fetched: number;
  new_records: number;
  updated_records: number;
  status: string;
  error_message: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface VoucherSummary {
  total: number;
  required_total: number;
  no_voucher_required: number;
  complete: number;
  missing_invoice: number;
  missing_receipt: number;
  pending_confirmation: number;
  receipt_total: number;
  unmatched_receipts: number;
}

export interface VoucherBatch {
  id: number;
  year_month: string;
  status: string;
  item_count: number;
  created_at: string;
  updated_at: string;
  summary: VoucherSummary;
}

export interface VoucherReceiptClearResult {
  ok: boolean;
  deleted_receipts: number;
  deleted_invoices?: number;
  removed_invoice_references?: number;
  deleted_links: number;
  file_cleanup_failed: number;
}

export interface VoucherAttachment {
  id: number;
  file_name: string;
  content_type: string;
  size: number;
  download_url?: string | null;
  uploaded_at: string;
  link_id?: number | null;
  printed?: boolean;
  referenced_from_approval?: boolean;
  from_receipt?: boolean;
  feishu_comment_status?: "pending" | "sending" | "succeeded" | "failed" | "uncertain" | null;
  feishu_comment_error?: string | null;
}

export interface VoucherSourceAttachment {
  name?: string | null;
  file_name?: string | null;
  url?: string | null;
  file_token?: string | null;
  type?: string | null;
}

export interface VoucherItem {
  id: number;
  voucher_no: string;
  approval_id: number;
  source_line_key?: string | null;
  source_line_label?: string | null;
  applicant_name: string | null;
  payee_name: string | null;
  expense_date: string | null;
  description: string | null;
  approval_details?: Array<{ label: string; value: string }>;
  amount: number;
  currency: string;
  requires_voucher: boolean;
  include_approval_in_monthly_pdf: boolean;
  voucher_exclusion_reason: string | null;
  status: string;
  invoice_attachment: VoucherAttachment | null;
  invoice_attachments: VoucherAttachment[];
  receipt_attachment: VoucherAttachment | null;
  receipt_attachments: VoucherAttachment[];
  source_attachment_candidates: VoucherSourceAttachment[];
  source_attachment_candidate_count: number;
  match_confidence: number | null;
  updated_at: string;
}

export interface VoucherItemListResponse {
  items: VoucherItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface VoucherMatchCandidate {
  id: number;
  receipt: VoucherAttachment | string;
  download_url?: string | null;
  amount: number | null;
  payee_name: string | null;
  transaction_date: string | null;
  bank_reference: string | null;
  score: number;
  reason: string;
  breakdown?: {
    amount?: number | { score?: number; reason?: string };
    payee?: number | { score?: number; reason?: string };
    date?: number | { score?: number; reason?: string };
  } | null;
}

export interface VoucherReceiptPoolItem {
  id: number;
  file_name: string;
  content_type?: string | null;
  size?: number | null;
  download_url?: string | null;
  uploaded_at: string;
  amount?: number | null;
  payee_name?: string | null;
  transaction_date?: string | null;
  bank_reference?: string | null;
  receipt_direction?: "income" | "outgoing" | "unknown" | null;
  receipt_category?: "pending" | "income" | "other" | null;
  linked_item_id?: number | null;
  voucher_no?: string | null;
  linked_voucher_no?: string | null;
  invoice_attachments: VoucherAttachment[];
  can_upload_invoice?: boolean;
  can_delete?: boolean;
}

export interface VoucherReceiptInvoiceCandidate {
  document_id: number;
  item_id: number;
  year_month: string;
  voucher_no: string;
  instance_code: string;
  definition_name?: string | null;
  applicant_name?: string | null;
  payee_name?: string | null;
  description?: string | null;
  amount?: number | null;
  currency: string;
  invoice_attachment: VoucherAttachment;
}

export interface VoucherIncompleteItem {
  item_id: number;
  voucher_no: string;
  approval_id: number;
  instance_code: string;
  definition_name?: string | null;
  applicant_name?: string | null;
  payee_name?: string | null;
  description?: string | null;
  amount?: number | null;
  amount_cny?: number | null;
  currency: string;
  date?: string | null;
  missing_invoice: boolean;
  missing_receipt: boolean;
}

export interface VoucherIncompleteMonth {
  batch_id: number;
  batch_status: string;
  year_month: string;
  count: number;
  missing_invoice: number;
  missing_receipt: number;
  items: VoucherIncompleteItem[];
}

export interface VoucherIncompleteResponse {
  total: number;
  missing_invoice: number;
  missing_receipt: number;
  months: VoucherIncompleteMonth[];
}

export interface VoucherSourceAttachmentResponse {
  item_id: number;
  metadata_only: boolean;
  attachments: VoucherSourceAttachment[];
  manual_upload_required: boolean;
}

export interface VoucherFeishuAttachmentImportResult {
  scanned: number;
  imported: number;
  duplicates: number;
  linked: number;
  skipped: number;
  failed: Array<{
    file_name: string;
    source: string;
    reason: string;
  }>;
}

export interface VoucherFeishuBatchAttachmentImportResult {
  configured: boolean;
  eligible_items: number;
  attempted_items: number;
  items_with_attachments: number;
  items_without_attachments: number;
  failed_items: number;
  deferred_items: number;
  scanned: number;
  imported: number;
  linked: number;
  duplicates: number;
  skipped: number;
  failed_count: number;
  failures: Array<{
    item_id: number;
    voucher_no: string;
    reason: string;
  }>;
}

export interface VoucherReceiptUploadResult {
  uploaded: number;
  matched: number;
  pending_confirmation: number;
  duplicates: number;
  split_pages?: number;
  failed: Array<{ file_name: string; reason: string }> | number;
}

export interface VoucherReceiptAutoMatchResult {
  scanned: number;
  matched: number;
  pending_confirmation: number;
}

export interface VoucherFeishuCommentSyncResult {
  eligible: number;
  synced: number;
  already_synced: number;
  failed: number;
  uncertain: number;
  processing: number;
  errors: Array<{
    invoice_document_id: number;
    file_name: string;
    status: "failed" | "uncertain";
    reason: string;
  }>;
}

export interface VoucherMatchConfirmResult {
  ok: boolean;
  link_id: number;
  item_id: number;
  document_id: number;
  feishu_comment_sync: VoucherFeishuCommentSyncResult;
}
