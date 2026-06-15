// Adobe ActiveX flash launcher (legacy IE).
// 原檔典藏自舊主機時為 404 HTML，已替換為 noop 防止 JS 解析錯誤。
// 現代瀏覽器使用 Ruffle 接管 Flash 播放。
function AC_FL_RunContent() { /* noop - Ruffle 接管 */ }
function AC_AX_RunContent() { /* noop */ }
function AC_Generateobj() { return ''; }
