# L3A Architecture Record

Tài liệu ghi lại toàn bộ thiết kế kiến trúc hệ thống Multi-Agent Day09 L3A (E-commerce Dispute Resolution). Mọi quyết định thiết kế đều hướng tới tính có thể kiểm chứng (verifiable), tuân thủ 100% Public Contracts, đảm bảo an toàn Provenance và tối ưu hóa 7 thành phần điểm số trong Scoring Policy V2. Tuyệt đối không ghi prompt bí mật hoặc hidden chain-of-thought.

---

## 1. Kiến trúc Agent (Agent Architecture)

### 1.1. System Overview & Luồng Handoff (Coordinator, Specialist Agents, Handoff Flow)

Hệ thống xử lý khiếu nại theo mô hình **Pipeline phân quyền kết hợp đồ thị có hướng không chu trình (A2A DAG - Directed Acyclic Graph)**:

```text
[inputs/<case_id>.json]
          │
          ▼
    Coordinator (Khởi tạo case, trích xuất thực thể sơ bộ, phân công task)
          │
          ├───► Order/Item Agent  ──[MCP: order, item, seller]──┐
          │                                                     │
          ├───► Payment Agent     ──[MCP: payment, refund]──────┤  (Specialist Collaboration)
          │                                                     │
          └───► Shipment Agent    ──[MCP: shipment, tracking]───┘
                                        │
                                        ▼
                                  Policy Agent  ──[MCP: policy, rules]
                                        │
                                        ▼
                                  Verifier Agent (Kiểm định Invariants & Cross-field Consistency)
                                        │
                                        ▼
    Coordinator (Tổng hợp & Hoàn tất) ──► outputs/<case_id>.json
          │                                     │
          └─────────────────► traces/trace.jsonl ◄───┘
```

- **Luồng dữ liệu**: Input case $\rightarrow$ Coordinator kích hoạt lifecycle $\rightarrow$ Các Specialist Agents gọi các MCP Tools thuộc domain được cấp quyền $\rightarrow$ Policy Agent tổng hợp đối soát điều khoản $\rightarrow$ Verifier Agent kiểm tra các ràng buộc logic & schema $\rightarrow$ Xuất file output và ghi nhận trace log đầy đủ.
- **Trace Observer**: Mọi sự kiện phối hợp, bàn giao, tiêu thụ bằng chứng đều được `TraceWriter` ghi tuần tự vào `traces/trace.jsonl`.

### 1.2. Quyền sở hữu & Phân quyền công cụ (Ownership & Tool Permissions)

Áp dụng nguyên tắc **Đặc quyền tối thiểu (Principle of Least Privilege)**: Mỗi agent chỉ được cấp quyền truy cập đúng tập tool thuộc phạm vi trách nhiệm nghiệp vụ của mình. Tuyệt đối không cho phép mọi agent truy vấn tự do toàn bộ MCP tools.

| Actor | Input | Trách nhiệm | Tool permissions | Output / Handoff |
| --- | --- | --- | --- | --- |
| **Coordinator** (`coordinator`) | `inputs/<case_id>.json` | Tiếp nhận case, emit `case_received`, bóc tách các trường định danh ban đầu, lập lịch và ủy quyền cho specialists qua `task_assigned`, nhận kết quả đã verify để emit `case_finalized` và lưu file output. | Không gọi domain tools (chỉ discovery tools nếu cần). | Phân phối nhiệm vụ điều tra kèm `case_id` cho `order-item-agent`. |
| **Order/Item Agent** (`order-item-agent`) | `case_id`, `order_id` (trích từ message/case) | Xác thực tính hợp lệ của đơn hàng, trạng thái vòng đời đơn (`order_status`), danh mục các items, thông tin người bán (`seller_id`), giá niêm yết và phụ phí. | `get_order`<br>`get_order_items`<br>`get_seller`<br>`get_product` | Bộ dữ liệu đơn hàng đã xác thực, danh sách `order_ids`, `item_ids`, `seller_ids`, `evidence_refs`, bàn giao sang `payment-agent` và `shipment-agent`. |
| **Payment Agent** (`payment-agent`) | `case_id`, `order_id`, payment references | Đối soát giao dịch thanh toán: phương thức, số đợt trả góp, tổng tiền thanh toán (`captured_total_brl`), phát hiện thanh toán trùng (`duplicate_charge`), lệch tiền (`payment_mismatch`), trạng thái hoàn tiền (`refund_pending`, `refund_failed`). | `get_payments`<br>`get_refunds` | Trạng thái thanh toán, `payment_references`, số tiền đã thanh toán/hoàn trả, `evidence_refs`, bàn giao dữ liệu tài chính cho `policy-agent`. |
| **Shipment Agent** (`shipment-agent`) | `case_id`, `order_id`, timeline vận chuyển | Phân tích hành trình giao hàng: so sánh `shipping_limit_date`, ngày bàn giao đơn vị vận chuyển (`carrier_delivered_date`), ngày giao thực tế (`delivered_customer_date`) và ngày dự kiến (`estimated_delivery_date`). Phân định trách nhiệm trễ hạn: do Seller giao muộn (`late_delivery_seller`) hay do Logistics (`late_delivery_logistics`), hàng thất lạc (`lost`). | `get_shipment`<br>`get_tracking` | Phán quyết vận chuyển (`shipment_verdict`), danh sách `shipment_ids`, `late_seller_ids`, `evidence_refs`, bàn giao sang `policy-agent`. |
| **Policy Agent** (`policy-agent`) | Báo cáo tổng hợp từ Order, Payment, Shipment, nội dung khiếu nại | Đối soát chính sách bồi hoàn của nền tảng, xác định `primary_issue` (11 enum), tìm nguyên nhân gốc rễ (`ranked_causes`), quy trách nhiệm (`responsible_parties`), tính số tiền hoàn trả (`financial_resolution`) bằng BRL, xác định các hành động khắc phục (`resolution_actions`). | `get_policy`<br>`get_dispute_rules` | Dự thảo đầy đủ của case output kèm toàn bộ `evidence_refs`, bàn giao sang `verifier`. |
| **Verifier** (`verifier`) | Dự thảo case output, case input gốc, tập hợp `evidence_refs` đã thu thập | Kiểm tra độc lập toàn diện: tính tuân thủ JSON schema `l3a-output-v2`, kiểm tra scope thực thể, tính nhất quán tài chính (tổng tiền hoàn = tổng refund lines), tính tương thích trách nhiệm và hiệu chuẩn `confidence`. Emit `verification_completed`. | Không có quyền gọi MCP tools (chỉ thực thi logic kiểm định thuần túy). | Kết quả thẩm định đạt chuẩn, chuyển lại cho `coordinator` để phát hành output. |

### 1.3. Thiết kế State Schema: `case_id`, `evidence_pool`, `decision`

State trung tâm của toàn bộ hệ thống được định nghĩa chặt chẽ trong mã nguồn qua class `CaseState` (file `src/student_agent/state.py`) hoạt động như một Blackboard pattern:

```python
class StateDict(TypedDict):
    case_id: str                              # Mã định danh case duy nhất (pattern: ^[A-Z0-9][A-Z0-9_-]{2,63}$)
    evidence_pool: dict[str, dict[str, Any]]  # Kho lưu trữ bằng chứng authoritative: evidence_ref -> envelope
    decision: dict[str, Any]                  # Dự thảo / Kết quả quyết định cuối cùng (khớp l3a-output-v2)
    context: dict[str, Any]                   # Bảng chia sẻ ngữ cảnh trung gian giữa các agents
```

- **`case_id`**: Khóa liên kết xuyên suốt toàn bộ lifecycle, bắt buộc khớp với input filename và mọi trace event.
- **`evidence_pool`**: Lưu trữ tập trung các bằng chứng được trả về từ MCP Gateway (`day09-mcp-evidence-v1`), đánh chỉ mục theo `evidence_ref`. Mọi truy xuất bằng chứng trong quá trình sinh output hay ghi trace đều lấy từ pool này, ngăn chặn ref ảo hoặc ref từ case khác.
- **`decision`**: Chứa trạng thái quyết định đang phát triển gồm: `assessment`, `affected_entities`, `root_cause_analysis`, `financial_resolution`, `resolution_actions`, và `claim_assessments`.
- **`context`**: Bộ nhớ đệm lưu các phát hiện trung gian (ví dụ: `order_data`, `payment_summary`, `shipping_timeline`) được chuyển giao giữa các agent.

---

## 2. A2A Message Contract

Để đảm bảo các Agent giao tiếp phi ghép nối (decoupled) và có cấu trúc chặt chẽ, hệ thống chuẩn hóa hai hợp đồng thông điệp Input và Output (đã cài đặt trong `src/student_agent/state.py`):

### 2.1. Input Schema cho mỗi Agent
Mọi Agent khi được gọi nhận một payload chuẩn:
```python
class AgentInputDict(TypedDict):
    task: str                 # Mô tả định danh tác vụ cần thực hiện (ví dụ: "investigate_order", "audit_payments")
    case_id: str              # Mã case đang xử lý để kiểm tra tính nhất quán và correlation
    context: dict[str, Any]   # Ngữ cảnh đầu vào: entities, thông tin khiếu nại, phát hiện của agent đi trước
```

### 2.2. Output Schema cho mỗi Agent
Mỗi Specialist Agent sau khi hoàn thành nhiệm vụ trả về một kết quả cấu trúc:
```python
class AgentOutputDict(TypedDict):
    evidence_refs: list[str]  # Danh sách các mã bằng chứng thu thập được từ MCP (pattern: ^ev_[A-Za-z0-9_-]{20,96}$)
    evidence_ref: str | None  # Mã bằng chứng đại diện (hoặc bằng chứng chính)
    findings: dict[str, Any]  # Kết quả phân tích nghiệp vụ chuyên sâu của agent
    confidence: float         # Điểm tin cậy của phân tích, chuẩn hóa trong đoạn [0.0, 1.0]
```

### 2.3. Handoff Protocol: Thứ tự, Retry, Error Handling

1. **Thứ tự thực thi (Execution Order)**:
   - **Bước 1**: `coordinator` nhận input case $\rightarrow$ emit `case_received` $\rightarrow$ tạo `CaseState` $\rightarrow$ emit `task_assigned` bàn giao cho `order-item-agent`.
   - **Bước 2**: `order-item-agent` truy vấn MCP (`get_order`, `get_order_items`, `get_seller`), lưu bằng chứng vào `evidence_pool`, emit `tool_result_consumed`, cập nhật context $\rightarrow$ emit `handoff` chuyển giao cho `payment-agent` và `shipment-agent`.
   - **Bước 3**: `payment-agent` và `shipment-agent` chạy độc lập truy vấn domain tương ứng, emit `tool_result_consumed`, cập nhật context $\rightarrow$ emit `handoff` chuyển tiếp cho `policy-agent`.
   - **Bước 4**: `policy-agent` tổng hợp context, tra cứu `get_policy`, xác định primary issue, root causes, tiền bồi hoàn $\rightarrow$ emit `policy_decided` $\rightarrow$ emit `handoff` chuyển sang `verifier`.
   - **Bước 5**: `verifier` thực hiện kiểm định Invariants $\rightarrow$ emit `verification_completed` $\rightarrow$ bàn giao kết quả về cho `coordinator`.
   - **Bước 6**: `coordinator` ghi file `outputs/<case_id>.json` và emit `case_finalized`.

2. **Cơ chế Retry (Retry Policy)**:
   - Áp dụng cơ chế **Exponential Backoff có Jitter** ($1.0s, 2.0s$) cho các lỗi đường truyền mạng hoặc HTTP 502/503/504 từ MCP Gateway.
   - Giới hạn tối đa **2 lần retry** cho mỗi lệnh gọi tool.
   - Thao tác retry đảm bảo tính **lũy đẳng (idempotent)** với cùng tham số (`case_id`, `entity_id`).

3. **Xử lý lỗi (Error Handling & Fallbacks)**:
   - **Lỗi thực thể không tồn tại (HTTP 404 / Empty Data)**: Đây là dữ liệu thẩm quyền xác nhận entity không có thật $\rightarrow$ Không retry, kết luận khiếu nại không có căn cứ (`unsupported_claim`), không hoàn tiền (`0.0 BRL`), emit `decision_code: "ENTITY_NOT_FOUND"`.
   - **Lỗi cạn kiệt Retry (MCP Timeout)**: Ghi nhận thiếu bằng chứng, tuyệt đối không suy đoán số liệu $\rightarrow$ Chuyển `primary_issue: "insufficient_evidence"`, `case_status: "needs_investigation"`, hạ `confidence: 0.3`, emit `decision_code: "MCP_TIMEOUT_FALLBACK"`.
   - **Lỗi mâu thuẫn dữ liệu (Source Conflict)**: Lấy dữ liệu authoritative từ MCP làm căn cứ; ghi nhận chi tiết đối chiếu vào mảng `data_conflicts` của output.
   - **Lỗi vi phạm kiểm định (Verifier Rejection)**: Tự động kích hoạt Safe Mode: đặt `case_status: "needs_investigation"`, hạ `confidence: 0.35`, điều chỉnh các trường tiền tệ để bảo đảm 100% schema compliance.

---

## 3. Observable Trace

Hệ thống tuân thủ nghiêm ngặt nguyên tắc **Khả năng quan sát minh bạch (Observable Trace)**, tuyệt đối không đưa prompt hoặc nội dung suy luận riêng (hidden chain-of-thought) vào trace log.

### 3.1. Định nghĩa Trace Events theo `trace-event-v1.schema.json`

Mọi sự kiện trace tuân thủ schema [`trace-event-v1.schema.json`](file:///d:/CRUD/K4-L3A-MultiAgent-MCP-A2A/contracts/schemas/trace-event-v1.schema.json):
- `schema_version`: `"day09-trace-event-v1"`
- `event_id`: Regex `^evt_[A-Za-z0-9_-]{12,96}$`
- `case_id`: Khóa liên kết case
- `event_type`: Thuộc 1 trong 7 loại sự kiện:
  `"case_received"`, `"task_assigned"`, `"tool_result_consumed"`, `"handoff"`, `"policy_decided"`, `"verification_completed"`, `"case_finalized"`.
  *(Trong đó 5 sự kiện bắt buộc theo Scoring Policy: `case_received`, `task_assigned`, `handoff`, `verification_completed`, `case_finalized`)*.
- `occurred_at`: Thời gian chuẩn ISO 8601 UTC dạng `YYYY-MM-DDTHH:MM:SSZ`.
- `actor`: Tên định danh agent thực thi.

### 3.2. Ghi nhận 3 hành vi cốt lõi qua TraceWriter API

Lớp `TraceWriter` (cài đặt trong `src/student_agent/trace.py`) cung cấp 3 phương thức chuyên biệt để ghi nhận:
1. **Agent Handoff**:
   ```python
   trace.emit_handoff(
       case_id=case_id,
       actor="order-item-agent",
       target="payment-agent",
       decision_code="ORDER_VERIFIED",
       attributes={"items_count": len(items)},
   )
   ```
2. **Tool Call / Result Consumption**:
   ```python
   trace.emit_tool_call(
       case_id=case_id,
       actor="order-item-agent",
       tool_name="get_order",
       evidence_refs=[evidence["evidence_ref"]],
       attributes={"order_status": "delivered"},
   )
   ```
3. **Decision Rationale**:
   ```python
   trace.emit_decision(
       case_id=case_id,
       actor="policy-agent",
       decision_code="FULL_REFUND_APPROVED",
       evidence_refs=policy_evidence_refs,
       attributes={"refund_amount_brl": 150.50},
   )
   ```

### 3.3. Xuất file `trace.jsonl`

- Đường dẫn đích: `traces/trace.jsonl`.
- Định dạng: Chuẩn **UTF-8**, **JSON Lines** (mỗi dòng là một đối tượng JSON độc lập, phân tách bằng dấu xuống dòng `\n`, không thụt lề thừa, dấu phân cách compact `separators=(",", ":")`).
- Tự động kiểm tra tính duy nhất của `event_id` và không chứa Team API Key.

---

## 4. Evidence lifecycle (Vòng đời của bằng chứng)

1. **Truy vấn & Validate Envelope**:
   - Mọi truy vấn MCP qua `gateway.call(tool_name, case_id=case_id, **params)` đều tự động validate phong bì trả về theo contract `mcp-evidence-response-v1.schema.json`.
   - Bắt buộc kiểm tra định dạng `evidence_ref` (`^ev_[A-Za-z0-9_-]{20,96}$`), tính toàn vẹn `result_hash` (`^sha256:[a-f0-9]{64}$`) và thuộc 1 trong 9 `domain` hợp lệ.
2. **Lưu trữ & Ánh xạ (Context Store)**:
   - Dữ liệu `evidence_ref` và payload `data` được lưu vào bộ nhớ cục bộ của phiên xử lý case hiện tại.
   - Ánh xạ rõ ràng từng `evidence_ref` vào các thực thể liên quan (`affected_entities`) và nhận định khiếu nại (`claim_assessments`).
3. **Phát hành Trace Event (`tool_result_consumed`)**:
   - Khi bất kỳ specialist agent nào sử dụng dữ liệu từ bằng chứng để đưa ra nhận định hoặc phân nhánh logic, agent đó phải emit ngay sự kiện `tool_result_consumed`:
     ```python
     trace.emit_tool_call(
         case_id=case["case_id"],
         actor="order-item-agent",
         tool_name="get_order",
         evidence_refs=[evidence["evidence_ref"]],
     )
     ```
4. **Cô lập phạm vi (Scope Isolation & Provenance)**:
   - Nghiêm cấm tuyệt đối việc tái sử dụng `evidence_ref` giữa các case khác nhau (Hard gate: `cross_scope_evidence_ref`).
   - Xóa sạch state sau khi kết thúc mỗi case để đảm bảo không bị rò rỉ dữ liệu hoặc bằng chứng chéo phiên.

---

## 5. Failure policy & Retry mechanism

| Tình huống lỗi (Failure) | Có Retry? | Chiến lược Fallback | Trace Event & Decision Code |
| --- | --- | --- | --- |
| **MCP Timeout / Network Flake** | **Có**: Tối đa 2 lần retry, áp dụng Exponential Backoff có Jitter ($1.0s, 2.0s$). Chỉ retry các lỗi đường truyền mạng hoặc HTTP 502/503/504. | Nếu sau 2 lần retry vẫn timeout: không bịa dữ liệu; ghi nhận thiếu bằng chứng cho domain đó; phân loại `primary_issue` thành `insufficient_evidence` với `case_status: "needs_investigation"` và `confidence: 0.3`. | `policy_decided` với `decision_code: "MCP_TIMEOUT_FALLBACK"` |
| **Entity Not Found (HTTP 404 / Empty Data)** | **Không**: Phản hồi Not Found từ MCP là kết quả thẩm quyền (authoritative) khẳng định thực thể không tồn tại trong hệ thống. | Kết luận khiếu nại của khách hàng là không có căn cứ (`unsupported_claim`), không hoàn tiền (`recommended_refund_brl: 0.0`), đề xuất hành động từ chối khiếu nại (`REJECT_CLAIM`). | `policy_decided` với `decision_code: "ENTITY_NOT_FOUND"` |
| **Source Conflict (Khách hàng nói khác MCP / Lệch giữa các MCP domain)** | **Không retry**: Đây là bài toán nghiệp vụ xử lý dữ liệu mâu thuẫn, không phải lỗi hệ thống. | Luôn lấy dữ liệu authoritative từ MCP Gateway làm chân lý; ghi nhận chi tiết mâu thuẫn vào mảng `data_conflicts` của output với `selected_source` và `resolution_code` tương ứng. | `policy_decided` với `decision_code: "DATA_CONFLICT_RESOLVED"` |
| **Invalid Specialist Result / Format Violation** | **Có**: Tối đa 1 lần yêu cầu Specialist Agent định dạng lại dữ liệu nếu vi phạm kiểu dữ liệu hoặc thiếu trường. | Nếu Verifier vẫn phát hiện vi phạm Invariant: tự động kích hoạt chế độ an toàn (Safe Mode): đặt `case_status: "needs_investigation"`, hạ `confidence: 0.35`, bảo toàn tính hợp lệ của schema. | `verification_completed` với `decision_code: "INVARIANT_CORRECTED"` |

**Nguyên tắc cốt lõi của Retry:**
- **Có giới hạn (Strictly Bounded)**: Số lần retry không vượt quá 2 lần để tránh cạn kiệt thời gian xử lý của cả pipeline.
- **Tính lũy đẳng (Idempotent)**: Retry chỉ gửi lại cùng tham số truy vấn (`case_id`, `entity_id`), không sinh ra tác dụng phụ.
- **Không suy diễn (Never Hallucinate)**: Thiếu bằng chứng thì phản ánh là thiếu (`insufficient_evidence`), tuyệt đối không tự tạo `evidence_ref` giả hoặc giả lập số liệu tài chính.

---

## 6. Verification invariants (Quy tắc kiểm định bắt buộc trước Finalize)

Verifier Agent bắt buộc phải thực thi và vượt qua 7 bài kiểm tra bất biến (Invariants) trước khi cho phép Coordinator phát hành kết quả:

1. **Schema Compliance**:
   - Dữ liệu output phải pass 100% qua `Draft202012Validator` với schema [`l3a-output-v2.schema.json`](file:///d:/CRUD/K4-L3A-MultiAgent-MCP-A2A/contracts/schemas/l3a-output-v2.schema.json). Không có trường lạ (`additionalProperties: false`).
2. **Entity Scope Integrity**:
   - Tất cả các ID (`order_ids`, `seller_ids`, `payment_references`, `shipment_ids`) trong `affected_entities` phải thuộc đúng phạm vi đơn hàng đang xử lý hoặc được trả về từ authoritative MCP tool trong case đó.
3. **Evidence Ownership & Provenance**:
   - Toàn bộ `evidence_refs` trong output phải là các mã `ev_...` thu được từ các lệnh gọi MCP hợp lệ của đúng case đó và đã được log qua sự kiện `tool_result_consumed`. Không chứa ref ảo hoặc ref từ case khác.
4. **Claim-Evidence Linkage**:
   - Với mọi claim trong `claim_assessments`, nếu phán quyết là `supported` hoặc `partially_supported`, bắt buộc phải có ít nhất 1 `evidence_ref` tương ứng làm căn cứ.
5. **Financial Consistency & Currency**:
   - `currency` luôn luôn là `"BRL"`.
   - `recommended_refund_brl` $\ge 0$.
   - Tổng số tiền hoàn của từng dòng trong `refund_lines` (`amount_brl`) phải bằng chính xác `recommended_refund_brl` (sai số tuyệt đối $< 0.01$).
   - Nếu `case_status == "no_action"`, thì `recommended_refund_brl` bắt buộc phải bằng `0.0` và `refund_lines` phải rỗng `[]`.
6. **Responsibility & Action Consistency**:
   - Nếu `primary_issue == "late_delivery_seller"`, `responsible_parties` phải chứa ít nhất 1 thực thể có `party_type: "seller"`.
   - Nếu `primary_issue == "late_delivery_logistics"`, `responsible_parties` phải chứa thực thể `party_type: "logistics_provider"`.
   - Nếu khiếu nại dẫn đến hoàn tiền (`recommended_refund_brl > 0`), mảng `resolution_actions` phải chứa hành động hoàn tiền (ví dụ: `"PROCESS_REFUND"`).
   - Mọi hành động trong `resolution_actions` phải là duy nhất (`uniqueItems: true`).
7. **Confidence Calibration**:
   - `confidence` luôn nằm trong khoảng $[0.0, 1.0]$.
   - Trường hợp đầy đủ bằng chứng đối soát rõ ràng: gán confidence cao ($0.85 - 0.98$).
   - Trường hợp thiếu bằng chứng hoặc dữ liệu mâu thuẫn phức tạp: gán confidence vừa phải ($0.30 - 0.60$) nhằm tối ưu hóa điểm số Calibration: $1 - (\text{correctness} - \text{confidence})^2$.

---

## 7. Reproducibility & Deployment configuration

- **Môi trường & Dependency Pinning**:
  - Python: $\ge 3.11$ (kiểm thử trên Python 3.12).
  - Dependencies: `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`.
  - Dev dependencies: `pytest>=8.4,<9`, `ruff>=0.12,<1`.
- **Concurrency & Resource Limits**:
  - Chạy xử lý tuần tự từng case hoặc sử dụng `asyncio.Semaphore(3)` khi chạy song song để đảm bảo không vi phạm rate-limit của MCP Gateway.
  - MCP Client HTTP Timeout: `timeout=300.0s`, `connect=30.0s`, `write=30.0s`, `pool=30.0s`.
- **Deterministic Execution**:
  - Các logic phân tích quy tắc, đối soát tài chính và gán mã lỗi hoàn toàn là hàm tiền định (deterministic functions), đảm bảo chạy lại nhiều lần cho ra kết quả đồng nhất 100%.
- **Quy trình lệnh chuẩn (CLI)**:
  ```bash
  # 1. Kiểm tra bộ dữ liệu đầu vào
  day09 validate-inputs

  # 2. Khám phá các công cụ MCP khả dụng
  day09 mcp-tools

  # 3. Chạy pipeline multi-agent xử lý tất cả các case
  day09 run

  # 4. Kiểm định tính hợp lệ của output và trace
  day09 validate

  # 5. Đóng gói file nộp bài chuẩn quy cách
  day09 package --output dist/submission.zip
  ```
