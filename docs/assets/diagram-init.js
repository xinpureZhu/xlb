/* ==========================================================================
   XLB 零售平台 · 文档中心 架构图初始化
   自托管 Mermaid 的统一配置。主题变量与 assets/style.css 的设计系统保持一致。
   用法（放在 </body> 前）：
     <script src="assets/mermaid.min.js"></script>
     <script src="assets/diagram-init.js"></script>
   ========================================================================== */
(function () {
  if (typeof mermaid === 'undefined') {
    // Mermaid 未加载时静默降级：pre.mermaid 保留图源码，正文仍完整可读。
    return;
  }
  mermaid.initialize({
    startOnLoad: true,
    theme: 'base',
    themeVariables: {
      background: '#f8fafc',
      primaryColor: '#ffffff',
      primaryTextColor: '#1e293b',
      primaryBorderColor: '#94a3b8',
      secondaryColor: '#eff6ff',
      tertiaryColor: '#f1f5f9',
      lineColor: '#64748b',
      textColor: '#1e293b',
      clusterBkg: '#eef2f7',
      clusterBorder: '#cbd5e1',
      nodeBorder: '#94a3b8',
      edgeLabelBackground: '#ffffff',
      fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", Helvetica, sans-serif',
      fontSize: '13px'
    },
    flowchart: { curve: 'basis', nodeSpacing: 45, rankSpacing: 55, useMaxWidth: true, htmlLabels: true },
    sequence: { useMaxWidth: true, wrap: true, actorMargin: 40 },
    state: { useMaxWidth: true },
    class: { useMaxWidth: true },
    er: { useMaxWidth: true }
  });
})();
