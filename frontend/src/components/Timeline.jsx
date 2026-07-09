import { TIMELINE_STEPS, statusMeta } from "../status.js";

/**
 * 项目进度时间线
 *
 * 根据项目状态渲染 8 个阶段节点：
 * 已完成的节点绿色，当前节点蓝色高亮，失败时当前节点红色。
 */
export default function Timeline({ status }) {
  const meta = statusMeta(status);
  const failed = meta.tone === "failed";
  // 失败时不知道具体卡在哪个阶段，把第一个节点标红并保留其余为灰
  const activeStep = failed ? 0 : meta.step;

  return (
    <div className="timeline">
      {TIMELINE_STEPS.map((label, i) => {
        let cls = "step";
        if (!failed && i < activeStep) cls += " done";
        if (!failed && i === activeStep) cls += " active";
        if (failed && i === activeStep) cls += " failed";
        return (
          <div className={cls} key={label}>
            <div className="dot">{!failed && i < activeStep ? "✓" : i + 1}</div>
            <div className="label">{label}</div>
          </div>
        );
      })}
    </div>
  );
}
