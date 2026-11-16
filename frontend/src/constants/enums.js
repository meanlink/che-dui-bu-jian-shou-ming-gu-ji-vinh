export const Role={admin:'业务负责人',analyst:'分析员',viewer:'观察员'};
export const JobStatus={queued:'排队中',running:'计算中',completed:'已完成',failed:'执行失败'};
export const StationStatus={in_sync:'两侧一致',awaiting_receipt:'等待回执',withdrawing:'撤销待核对',drift:'版本不一致',offline:'站端离线',disabled:'站点停用'};
export const DirectiveStatus={pending:'待下发',retrying:'重试中',receipt_lost:'回执丢失待查',effective:'已生效',superseded:'已被取代',revoked:'已撤销',failed:'发送失败',blocked:'站点停用'};
export const DirectiveKind={publish:'下发',revoke:'撤销'};
