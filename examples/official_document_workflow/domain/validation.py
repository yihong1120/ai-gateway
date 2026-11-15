"""Public content issues and the bounded correction policy for pure validators."""
from __future__ import annotations

import re


ISSUES = {
    'source_evidence_capacity_exceeded': ('長附件需保留的相關原文證據超過單次整合容量。', '請依主公文及相關附件拆分處理，保留完整原文後重新核對。', False),
    'source_reading_unverified': ('長附件的分段閱讀結果未通過完整性或來源範圍核對。', '請核對來源附件及分段閱讀結果；未使用不完整的內容建立行程。', False),
    'analysis_contract_invalid': ('模型回應的公文欄位格式不完整。', '依原文重新確認欄位；仍失敗時請人工核對。', True),
    'source_coverage_incomplete': ('模型未確認讀取全部附件。', '重新核對全部附件，不使用不完整的判讀結果。', True),
    'document_group_ambiguous': ('附件中有多份主公文，或無法確認主文與附件的關係。', '請將不同主公文分組選取，再各自處理。', False),
    'document_metadata_unverified': ('公文日期、文號或主旨未通過原文核對。', '核對主公文的日期、文號、主旨及掃描內容。', True),
    'event_date_evidence_mismatch': ('行程日期與引用的原文不一致。', '核對原文日期及引用段落；沒有明確日期時補充實際辦理日或送達日。', True),
    'event_time_evidence_mismatch': ('行程時刻與引用的原文不一致。', '核對原文的開始、結束時刻及跨日關係。', True),
    'event_source_reference_invalid': ('行程引用的附件或原文行號無法通過核對。', '依本次附件重新選擇原文依據，不借用其他文件的內容。', True),
    'event_fields_unverified': ('行程事項、工程或責任欄位未通過原文核對。', '核對該項工作的原文、工程名稱及辦理對象。', True),
    'event_relation_unconfirmed': ('行程日期或附件關係仍不明確。', '確認實際日期及適用文件後，再處理這筆行程。', False),
    'event_capacity_exceeded': ('行程數量超過本次可處理範圍。', '請依主公文分批處理，保留全部行程。', False),
    'event_interval_invalid': ('行程起訖時間或跨日關係未通過核對。', '確認實際起訖時間；不以修改原文日期消除錯誤。', True),
    'event_repair_omitted_items': ('模型更正時省略了原有行程。', '請人工核對所有原有行程，不將漏項當成處理成功。', False),
    'calendar_review_invalid': ('行事曆核對結果未通過來源驗證。', '請核對事件及日期依據；已產生的文件仍可下載。', False),
}


class ContentValidationError(ValueError):
    """Fixed public text; never persist arbitrary model/source exception text."""

    def __init__(self, code, *, event_index=None, field=None, group_index=None, repair_attempted=False):
        self.code = code
        self.event_index, self.field, self.group_index = event_index, field, group_index
        self.repair_attempted = repair_attempted
        self.reason, self.next_action, self.can_auto_repair = ISSUES[code]
        super().__init__(self.reason)

    def as_issue(self):
        return {
            'code': self.code, 'reason': self.reason, 'next_action': self.next_action,
            'group_index': self.group_index, 'event_index': self.event_index,
            'field': self.field, 'repair_attempted': self.repair_attempted,
            'can_auto_repair': self.can_auto_repair,
        }

    def describe(self):
        prefix = f'行程 {self.event_index}：' if self.event_index else ''
        return prefix + self.reason + self.next_action


def content_issue(error, *, metadata=False, group_index=None, repair_attempted=False):
    """Recognize explicit content checks only, never arbitrary ValueError bugs.

    Call solely around pure document/calendar validators. Transport, storage,
    authentication and model invocation failures are outside this boundary.
    """
    message = str(error)
    event = re.search(r'行程 (\d+)', message)
    field = re.search(r'的 (start_date|end_date|start_time|end_time|project_name|location|responsible_party) ', message)
    code = None
    if metadata:
        code = 'document_metadata_unverified'
    elif message == '行事曆契約版本必須為 2，未使用舊格式判讀。':
        code = 'analysis_contract_invalid'
    elif message == '整組判讀未確認所有附件，請檢查；未使用部分內容冒充完整判讀。':
        code = 'source_coverage_incomplete'
    elif message == '無法確認唯一主公文及附件關係，請人工確認或調整選取範圍。':
        code = 'document_group_ambiguous'
    elif message == '本組判讀文號或主文與分組不符，未套用其他函文的欄位。':
        code = 'document_metadata_unverified'
    elif message == '公文判讀 Agent 回應缺少 events。':
        code = 'analysis_contract_invalid'
    elif message == '格式更正省略了先前行程，請人工核對；未當成無事件完成。':
        code = 'event_repair_omitted_items'
    elif message == '行程陣列格式或數量不符。':
        code = 'event_capacity_exceeded'
    elif field and ('所引原文不符' in message or '缺少逐字來源' in message):
        code = (
            'event_date_evidence_mismatch' if field[1].endswith('_date') else
            'event_time_evidence_mismatch' if field[1].endswith('_time') else 'event_fields_unverified'
        )
    elif message.startswith((
        '行程欄位缺少有效的來源行號', '行程引用未授權或不存在的附件來源',
        '行程來源行號越界', '行程來源只有空白或系統標記', '行程原文包含來源未確認的版面標記',
    )):
        code = 'event_source_reference_invalid'
    elif event and any(text in message for text in ('缺少欄位來源', '缺少逐字來源', '工地名稱缺少逐字來源')):
        code = 'event_source_reference_invalid'
    elif event and any(text in message for text in ('缺少具體工作', '精簡事項名稱無效', '工作摘要超過 120 字', '來源關係格式無效', '辦理責任與來源關係不一致')):
        code = 'event_fields_unverified'
    elif event and '日期或附件關係待確認' in message:
        code = 'event_relation_unconfirmed'
    elif any(text in message for text in ('起訖資料無效', '結束時間早於起始時間', '與同一事件的結束時間衝突', '原文明載跨日結束時間')):
        code = 'event_interval_invalid'
    elif message.startswith(('行事曆語意核對回應格式無效', '行事曆合併對應無效', '非合併行程不得指定合併對象', '合併行程的工程或起訖不一致', '追蹤提醒與原文明定行程不可合併')):
        code = 'calendar_review_invalid'
    elif message.startswith((
        '相對期限推估欄位格式不完整', '相對期限的天數', '相對期限推估不能取代',
        'AI 追蹤提醒的天數或理由格式無效', 'AI 追蹤提醒缺少可確認的工作原文',
    )):
        code = 'event_fields_unverified'
    if code is None:
        return None
    return ContentValidationError(
        code, group_index=group_index, event_index=int(event[1]) if event else None,
        field=field[1] if field else None, repair_attempted=repair_attempted,
    )
