"""跨模块唯一来源的常量：路径、规则版本、阈值、列契约、空值哨兵。"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = BASE_DIR / "sample_data"

APP_NAME = "供应商主数据台账"
RULE_VERSION = "em-1.3.0"

DB_PATH = Path(os.environ.get("MERGE_DB", str(BASE_DIR / "data" / "merge.db")))

# 采信顺序固定，保证同一批文件得到同一个输入摘要与 run_id。
SAMPLE_FILES = (
    "erp_a_primary_utf8.csv",
    "erp_b_alias_bom.csv",
    "erp_c_north_gb18030.csv",
    "erp_d_amounts.csv",
    "erp_e_header_only.csv",
    "erp_f_blank_rows.csv",
)

# 编码复用接替演示批次：在六份主档之上再加一份带主体状态与未结余额的名单。
# 不并入默认批次，避免改变既有样例的对账数字。
SUCCESSION_FILES = SAMPLE_FILES + ("succession_g_lifecycle.csv",)

ENCODING_CHAIN = ("utf-8-sig", "utf-8", "gb18030")

# 列契约：字段名 -> (是否必填, 说明单位)
COLUMNS = (
    ("supplier_id", True),
    ("supplier_no", True),
    ("name", True),
    ("tax_id", False),
    ("city", False),
    ("bank_account", False),
    ("erp_source", False),
    ("ref_master_id", False),
    ("ytd_amount", False),
    ("last_update", False),
    # 主体生命周期与未结余额：可选列，缺列时按“未提供”处理，不影响旧批次。
    ("entity_status", False),
    ("open_balance", False),
)

COLUMN_ALIASES = {
    "供应商编码": "supplier_id",
    "供应商号": "supplier_id",
    "供应商编号": "supplier_no",
    "编码": "supplier_no",
    "名称": "name",
    "供应商名称": "name",
    "税号": "tax_id",
    "纳税人识别号": "tax_id",
    "城市": "city",
    "开户行账号": "bank_account",
    "来源系统": "erp_source",
    "主档号": "ref_master_id",
    "本年累计": "ytd_amount",
    "更新时间": "last_update",
    "主体状态": "entity_status",
    "状态": "entity_status",
    "未结余额": "open_balance",
    "余额": "open_balance",
}

# 空值哨兵：集中定义，与"空串"语义分开（空串表示确实没有税号）。
SENTINEL_VALUES = frozenset({"-", "--", "n/a", "na", "null", "none", "未知", "无", "空", "/"})

SIMILARITY_WEIGHT_DICE = Decimal("0.55")
SIMILARITY_WEIGHT_RATIO = Decimal("0.45")
AUTO_THRESHOLD = Decimal("0.92")
REVIEW_THRESHOLD = Decimal("0.80")
SCORE_SCALE = Decimal("0.000001")

MAX_BLOCK_SIZE = 120
MAX_CANDIDATES = 4000

TZ_DISPLAY = "Asia/Shanghai"
PAGE_SIZE = 60

# 编码复用接替判定：
# - 只有新主体带“受让/接替”等明确证据，或旧主体状态为注销/吊销/停业且
#   名称为同一主体时才判接替；仅名称相近不足以让旧账随编码转给新主体。
# - 新主体税号与老主体不同是默认前提；税号相同则属于同一主体改名/补录，
#   走普通合并，不进入接替流程。
SUCCESSION_NAME_THRESHOLD = Decimal("0.92")
DORMANT_STATUSES = frozenset({"注销", "已注销", "吊销", "已吊销", "停业", "已停业",
                              "撤销", "已撤销", "注销登记", "歇业"})
ACTIVE_STATUSES = frozenset({"存续", "在营", "开业", "正常", "在业", "存续（在营、开业、在册）"})
# 明确的主体接替证据词：新主体名称或来源系统备注中出现即视为集团重新分配。
SUCCESSION_MARKERS = ("接替", "承继", "继受", "受让", "承接", "并入", "吸收合并")
# 新主体编码复用旧编码时，允许的“名称相近”仅为辅助证据，不单独成立接替。
SUCCESSION_EVIDENCE_MIN = 2
