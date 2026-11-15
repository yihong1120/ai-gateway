using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Security.Principal;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace ChangdarAI {
    internal static class Package {
        internal const string RootPin = "338C3415CB3E60F67A7E50010E8D65EA2A30EF634F6C0A3009D391C86EC7BFFF";
        internal const string PublisherPin = "CCE5A188EC6E254BDFE3975645F5270C1F506F48491BC8B2F4B5C1D42A7CD24D";
        internal static readonly string[] Scripts = {"Setup-CompanyAI.ps1", "Get-CompanyAIKey.ps1", "Start-CompanyCodex.ps1"};
        internal static string Hash(byte[] data) { using (var h = SHA256.Create()) return BitConverter.ToString(h.ComputeHash(data)).Replace("-", ""); }
        internal static byte[] Resource(string name) {
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload." + name)) {
                if (s == null) throw new InvalidOperationException("安裝包缺少必要檔案：" + name);
                using (var output = new MemoryStream()) { s.CopyTo(output); return output.ToArray(); }
            }
        }
        internal static X509Certificate2 Certificate(string name, string pin) {
            var cert = new X509Certificate2(Resource(name));
            if (Hash(cert.RawData) != pin || cert.HasPrivateKey || DateTime.Now < cert.NotBefore || DateTime.Now >= cert.NotAfter)
                throw new InvalidOperationException("公司憑證的指紋或有效期限不符。");
            var eku = cert.Extensions.OfType<X509EnhancedKeyUsageExtension>().Single();
            if (eku.EnhancedKeyUsages.Count != 1 || eku.EnhancedKeyUsages[0].Value != "1.3.6.1.5.5.7.3.3")
                throw new InvalidOperationException("公司憑證必須僅供程式碼簽署。");
            return cert;
        }
        internal static void ValidateResources() {
            var json = new JavaScriptSerializer().Deserialize<Dictionary<string,string>>(System.Text.Encoding.UTF8.GetString(Resource("payload-hashes.json")));
            if (json.Count != Scripts.Length + 2 || !json.Keys.OrderBy(x=>x).SequenceEqual(Scripts.Concat(new[]{"RootCA.cer","Publisher.cer"}).OrderBy(x=>x)))
                throw new InvalidOperationException("安裝包清單不符。");
            foreach (var entry in json) if (Hash(Resource(entry.Key)) != entry.Value.ToUpperInvariant())
                throw new InvalidOperationException("安裝包內容驗證失敗：" + entry.Key);
            using (var root = Certificate("RootCA.cer",RootPin))
            using (var publisher = Certificate("Publisher.cer",PublisherPin)) {
                var basic = root.Extensions.OfType<X509BasicConstraintsExtension>().Single();
                var leaf = publisher.Extensions.OfType<X509BasicConstraintsExtension>().Single();
                if (!basic.CertificateAuthority || !basic.HasPathLengthConstraint || basic.PathLengthConstraint != 0 || leaf.CertificateAuthority || publisher.Issuer != root.Subject)
                    throw new InvalidOperationException("公司憑證的用途或簽發關係不符。");
            }
        }
        internal static string Extract() {
            ValidateResources();
            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"changdar-ai","installer");
            RejectLinks(root);
            Directory.CreateDirectory(root);
            string directory = Path.Combine(root,Guid.NewGuid().ToString("N"));
            var sid = WindowsIdentity.GetCurrent().User;
            var acl = new DirectorySecurity();
            acl.SetAccessRuleProtection(true,false); acl.SetOwner(sid);
            acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            Directory.CreateDirectory(directory,acl);
            foreach (string file in Scripts) {
                byte[] data = Resource(file);
                using (var stream = new FileStream(Path.Combine(directory,file),FileMode.CreateNew,FileAccess.Write,FileShare.None)) stream.Write(data,0,data.Length);
            }
            return directory;
        }
        internal static void RejectLinks(string path) {
            for (var current = new DirectoryInfo(path); current != null; current = current.Parent)
                if (current.Exists && (current.Attributes & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("安裝目錄不可使用連結，請聯絡管理員。");
        }
        internal static void Verify(string path) {
            using (var cert = new X509Certificate2(X509Certificate.CreateFromSignedFile(path)))
                if (Hash(cert.RawData) != PublisherPin) throw new InvalidOperationException("檔案不是由 changdar-ai 核准的憑證簽署。");
            Native.Verify(path);
        }
        internal static void VerifyAll(string directory) {
            Verify(Assembly.GetExecutingAssembly().Location);
            foreach (string file in Scripts) Verify(Path.Combine(directory,file));
        }
        internal static void InstallUserTrust(string directory) {
            // An explicit user action provisions only this Windows account.
            using (var root = Certificate("RootCA.cer",RootPin))
            using (var leaf = Certificate("Publisher.cer",PublisherPin))
            using (var roots = new X509Store(StoreName.Root,StoreLocation.CurrentUser))
            using (var publishers = new X509Store(StoreName.TrustedPublisher,StoreLocation.CurrentUser)) {
                roots.Open(OpenFlags.ReadWrite); publishers.Open(OpenFlags.ReadWrite);
                bool addRoot = roots.Certificates.Find(X509FindType.FindByThumbprint,root.Thumbprint,false).Count == 0;
                bool addLeaf = publishers.Certificates.Find(X509FindType.FindByThumbprint,leaf.Thumbprint,false).Count == 0;
                try {
                    if (addRoot) roots.Add(root);
                    using (var chain = new X509Chain()) {
                        chain.ChainPolicy.RevocationMode = X509RevocationMode.Online;
                        chain.ChainPolicy.RevocationFlag = X509RevocationFlag.ExcludeRoot;
                        chain.ChainPolicy.UrlRetrievalTimeout = TimeSpan.FromSeconds(30);
                        chain.ChainPolicy.ApplicationPolicy.Add(new Oid("1.3.6.1.5.5.7.3.3"));
                        if (!chain.Build(leaf) || chain.ChainElements.Count != 2 || Hash(chain.ChainElements[1].Certificate.RawData) != RootPin)
                            throw new InvalidOperationException("公司憑證鏈或撤銷檢查失敗。請確認公司內網／VPN 與 DNS，或聯絡 IT。");
                    }
                    // Check the actual PE and script signatures, not just their embedded certificates.
                    VerifyAll(directory);
                    if (addLeaf) publishers.Add(leaf);
                } catch {
                    if (addLeaf) publishers.Remove(leaf);
                    if (addRoot) roots.Remove(root);
                    throw;
                }
            }
        }
    }

    internal static class Native {
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]
        private struct FileInfo { public uint Size; [MarshalAs(UnmanagedType.LPWStr)] public string Path; public IntPtr Handle,Subject; }
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]
        private struct TrustData {
            public uint Size; public IntPtr Policy,Sip; public uint UI,Revocation,Choice; public IntPtr File;
            public uint Action; public IntPtr State,Url; public uint Flags,Context; public IntPtr Signature;
        }
        [DllImport("wintrust.dll",ExactSpelling=true,CharSet=CharSet.Unicode)]
        private static extern int WinVerifyTrust(IntPtr window,ref Guid action,ref TrustData data);
        internal static void Verify(string path) {
            var info = new FileInfo { Size=(uint)Marshal.SizeOf(typeof(FileInfo)),Path=path };
            IntPtr pointer = Marshal.AllocHGlobal(Marshal.SizeOf(info));
            Marshal.StructureToPtr(info,pointer,false);
            var data = new TrustData {Size=(uint)Marshal.SizeOf(typeof(TrustData)),UI=2,Revocation=1,Choice=1,File=pointer,Action=1,Flags=0x80};
            var action = new Guid("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");
            try {
                int status=WinVerifyTrust(new IntPtr(-1),ref action,ref data);
                if (status!=0) throw new InvalidOperationException("Windows 簽章驗證失敗（0x"+status.ToString("X8")+"）。請確認公司網路與憑證信任。");
            } finally {
                data.Action=2; WinVerifyTrust(new IntPtr(-1),ref action,ref data);
                Marshal.DestroyStructure(pointer,typeof(FileInfo)); Marshal.FreeHGlobal(pointer);
            }
        }
    }

    internal sealed class SetupForm : Form {
        private readonly CheckBox claude = new CheckBox {Text="Claude Code",Checked=true,AutoSize=true};
        private readonly CheckBox codex = new CheckBox {Text="Codex",Checked=true,AutoSize=true};
        private readonly CheckBox consent = new CheckBox {Text="我同意在此 Windows 帳號信任 changdar-ai，並設定選取的工具。",AutoSize=true};
        private readonly CheckBox replace = new CheckBox {Text="將既有個人／其他供應商設定切換為公司設定（原設定會備份）",AutoSize=true};
        private readonly Button start = new Button {Text="開始設定",Width=150,Height=38,Enabled=false};
        private readonly Button cancel = new Button {Text="取消設定",Width=120,Height=38,Enabled=false};
        private readonly Button browser = new Button {Text="開啟核准頁",Width=140,Height=38,Enabled=false};
        private readonly Button copy = new Button {Text="複製核准連結",Width=150,Height=38,Enabled=false};
        private readonly Label status = new Label {AutoSize=true,MaximumSize=new Size(640,0),Text="請連上公司內網或核准的 VPN。"};
        private readonly Label approval = new Label {AutoSize=true,MaximumSize=new Size(640,0)};
        private readonly TextBox progress = new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Width=640,Height=180};
        private readonly Timer timer = new Timer {Interval=500};
        private bool running, cancellationRequested;
        private string directory, approvalUrl, lastStatus, failureCode, currentStage;
        private static readonly Dictionary<string,string> Failures = new Dictionary<string,string> {
            {"APPROVAL_EXPIRED","核准已逾時。按「重新設定」取得新的驗證碼；已安裝的工具會保留。"},
            {"APPROVAL_FAILED","無法完成瀏覽器核准。請確認公司網路，並在新核准頁登入、核對驗證碼及按下核准。"},
            {"PROVIDER_CONFIG","既有 Claude 設定需要處理。若要切換公司服務，請勾選「將既有設定切換為公司設定」；若仍失敗，請由 IT 檢查設定檔格式或系統層級憑證。"},
            {"COMPANY_NETWORK","無法通過公司網路檢查。請連上公司內網或核准 VPN，確認公司 DNS 後按「重新設定」。"},
            {"TOOL_INSTALL","開發工具下載或安裝未完成。請確認可連線官方下載站，再按「重新設定」；已成功安裝的工具會略過。"},
            {"CODEX_VERSION","已安裝的 Codex 版本與公司核准版本不同。請由 IT 更新；既有安裝已保留。"},
            {"NETWORK_FAILED","公司服務暫時無法連線，重試仍未成功。請確認內網／VPN 後按「重新設定」。"},
            {"ACTIVATION_UNCERTAIN","尚未確認啟用結果，新的設定已保留。請恢復公司網路後重新設定；舊憑證可能已失效。"},
            {"CANCELLED","已取消設定。已安裝的工具會保留；可按「重新設定」繼續。"},
            {"SETUP_FAILED","設定未完成。請查看下方失敗階段與記錄，或交由 IT 處理後重新設定。"}
        };
        internal SetupForm() {
            Text="changdar-ai 設定工具";Font=new Font("Microsoft JhengHei UI",10);ClientSize=new Size(740,800);
            MinimumSize=new Size(720,500);StartPosition=FormStartPosition.CenterScreen;AutoScaleMode=AutoScaleMode.Dpi;
            var layout=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.TopDown,WrapContents=false,Padding=new Padding(28),AutoScroll=true};
            layout.Controls.Add(new Label {Text="changdar-ai",Font=new Font(Font.FontFamily,25,FontStyle.Bold),AutoSize=true,Margin=new Padding(0,0,0,14)});
            layout.Controls.Add(new Label {Text="安裝並連接公司 AI 開發工具\n已安裝的工具會略過下載；公司核准版本仍會進行檢查。",AutoSize=true,Margin=new Padding(0,0,0,18)});
            layout.Controls.Add(claude);layout.Controls.Add(codex);layout.Controls.Add(replace);
            layout.Controls.Add(new Label {Text="開始後會加入公司的公開簽署憑證，並在下方顯示設定進度。\n瀏覽器出現驗證碼時，請核對後親自核准；金鑰不會顯示在網頁。",AutoSize=true,Margin=new Padding(0,18,0,10)});
            var details=new LinkLabel {Text="查看公司憑證指紋",AutoSize=true};
            details.LinkClicked+=(s,e)=>MessageBox.Show(this,"僅用於程式碼簽署；只加入目前帳號。\n\n根憑證 SHA256：\n"+Package.RootPin+"\n\n發行者 SHA256：\n"+Package.PublisherPin,"changdar-ai 憑證",MessageBoxButtons.OK,MessageBoxIcon.Information);
            var actions=new FlowLayoutPanel {AutoSize=true,WrapContents=false};actions.Controls.Add(start);actions.Controls.Add(cancel);
            var links=new FlowLayoutPanel {AutoSize=true,WrapContents=false};links.Controls.Add(browser);links.Controls.Add(copy);
            layout.Controls.Add(details);layout.Controls.Add(consent);layout.Controls.Add(actions);layout.Controls.Add(status);layout.Controls.Add(approval);layout.Controls.Add(links);layout.Controls.Add(progress);Controls.Add(layout);
            EventHandler update=(s,e)=>start.Enabled=!running&&consent.Checked&&(claude.Checked||codex.Checked);
            consent.CheckedChanged+=update;claude.CheckedChanged+=update;codex.CheckedChanged+=update;
            start.Click+=async(s,e)=>await Run();cancel.Click+=(s,e)=>RequestCancellation();
            browser.Click+=(s,e)=>{if(approvalUrl!=null)try{Process.Start(new ProcessStartInfo(approvalUrl){UseShellExecute=true});}catch{status.Text="無法自動開啟瀏覽器，請按「複製核准連結」自行開啟。";}};
            copy.Click+=(s,e)=>{if(approvalUrl!=null)try{Clipboard.SetText(approvalUrl);}catch{status.Text="無法使用剪貼簿，請從下方記錄複製核准連結。";}};
            timer.Tick+=(s,e)=>ReadStatus();
            FormClosing+=(s,e)=>{if(running){e.Cancel=true;if(MessageBox.Show(this,"設定仍在執行。是否要求安全取消？程式會等目前步驟結束，避免中斷憑證啟用。","changdar-ai",MessageBoxButtons.YesNo,MessageBoxIcon.Question)==DialogResult.Yes)RequestCancellation();}};
            FormClosed+=(s,e)=>timer.Dispose();
        }
        private void RequestCancellation() {
            if(!running||directory==null)return;
            try {File.WriteAllText(Path.Combine(directory,"cancel.request"),"cancel");cancellationRequested=true;cancel.Enabled=false;status.Text="已要求取消，正在等待目前步驟安全結束；請保持視窗開啟。";}
            catch {status.Text="無法送出取消要求，請保持視窗開啟並等待目前步驟結束。";}
        }
        private static string Value(Dictionary<string,object> value,string key) {object found;return value.TryGetValue(key,out found)?Convert.ToString(found):"";}
        private void ReadStatus() {
            if(directory==null)return;
            try {
                string path=Path.Combine(directory,"status.json");if(!File.Exists(path)||new FileInfo(path).Length>16384)return;
                string text;using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
                using(var reader=new StreamReader(stream))text=reader.ReadToEnd();if(text==lastStatus)return;
                var value=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(text);ApplyStatus(value);lastStatus=text;
            } catch(IOException) {} catch(UnauthorizedAccessException) {} catch(ArgumentException) {} catch(InvalidOperationException) {}
        }
        private void ApplyStatus(Dictionary<string,object> value) {
            string kind=Value(value,"kind"),stage=Value(value,"stage");currentStage=stage;
            cancel.Enabled=running&&!cancellationRequested&&Value(value,"can_cancel").Equals("True",StringComparison.OrdinalIgnoreCase);
            string link=Value(value,"approval_url"),code=Value(value,"user_code");Uri uri;
            bool valid=kind=="approval"&&Uri.TryCreate(link,UriKind.Absolute,out uri)&&uri.Scheme=="https"&&uri.Port==443&&string.IsNullOrEmpty(uri.UserInfo)&&
                new[]{"changdar-server.mooo.com","ai-dev.changdar-server.mooo.com","ai.changdar-server.mooo.com"}.Contains(uri.Host)&&uri.AbsolutePath=="/company-ai/setup"&&
                System.Text.RegularExpressions.Regex.IsMatch(code,"^[A-F0-9]{4}-[A-F0-9]{4}$");
            approvalUrl=valid?link:null;browser.Enabled=copy.Enabled=valid&&running;
            approval.Text="";
            if(valid) {
                int remaining;int.TryParse(Value(value,"remaining"),out remaining);remaining=Math.Max(0,remaining);
                approval.Text="驗證碼："+code+"　剩餘 "+(remaining/60)+" 分 "+(remaining%60)+" 秒\n請在瀏覽器核對此驗證碼，親自按「核准此電腦」。";
                status.Text="等待瀏覽器核准；只開啟網頁尚未完成核准。";
            } else if(kind=="failed") {failureCode=Value(value,"code");status.Text=FailureMessage(failureCode);}
            else if(kind=="retry")status.Text="連線暫時失敗，正在自動重試（最多重試兩次）…";
            else if(kind=="complete")status.Text="設定完成。請完整關閉所有 Windows Terminal／PowerShell 視窗，再重新開啟並輸入 claude 或 codex。";
            else if(stage.IndexOf("Claude",StringComparison.Ordinal)>=0)status.Text="正在檢查或安裝 Claude Code；下載時間依網路速度而定…";
            else if(stage.IndexOf("Codex",StringComparison.Ordinal)>=0)status.Text="正在檢查或安裝 Codex；下載時間依網路速度而定…";
            else if(stage=="company VPN/DNS check")status.Text="正在檢查公司內網／VPN 與 DNS…";
            else if(stage=="creating device enrollment")status.Text="正在建立電腦綁定，稍後會開啟瀏覽器…";
            else if(stage=="acknowledging activation")status.Text="正在確認啟用結果，請勿關閉視窗…";
            else status.Text="正在驗證並儲存公司設定，請稍候…";
            if(cancellationRequested&&kind!="complete"&&kind!="failed")status.Text="已要求取消，正在等待目前步驟安全結束；請保持視窗開啟。";
        }
        private static string FailureMessage(string code) {string message;return Failures.TryGetValue(code??"",out message)?message:Failures["SETUP_FAILED"];}
        internal static void TestUI(string report) {
            using(var form=new SetupForm()) {
                form.running=true;
                string link="https://changdar-server.mooo.com/company-ai/setup?request_id=00000000-0000-0000-0000-000000000001&user_code=ABCD-1234";
                form.ApplyStatus(new Dictionary<string,object>{{"kind","approval"},{"stage","browser approval and device polling"},{"can_cancel",true},{"remaining",598},{"user_code","ABCD-1234"},{"approval_url",link}});
                if(!form.browser.Enabled||!form.copy.Enabled||!form.cancel.Enabled||!form.approval.Text.Contains("9 分 58 秒"))throw new Exception("Approval UI regression");
                form.ApplyStatus(new Dictionary<string,object>{{"kind","approval"},{"approval_url","https://evil.example/"},{"user_code","ABCD-1234"}});
                if(form.browser.Enabled||form.copy.Enabled)throw new Exception("Unapproved URL accepted");
                foreach(string code in Failures.Keys) {
                    form.ApplyStatus(new Dictionary<string,object>{{"kind","failed"},{"code",code},{"can_cancel",false}});
                    if(form.status.Text!=Failures[code]||form.browser.Enabled||form.cancel.Enabled)throw new Exception("Failure UI regression");
                }
                File.WriteAllText(report,new JavaScriptSerializer().Serialize(new{approval_countdown=true,approved_link_buttons=true,unapproved_link_blocked=true,failure_codes=Failures.Count,trust_changed=false,enrollment_created=false}));
            }
        }
        private async Task Run() {
            running=true;cancellationRequested=false;directory=null;lastStatus=failureCode=currentStage=null;approvalUrl=null;approval.Text="";progress.Clear();
            start.Enabled=cancel.Enabled=browser.Enabled=copy.Enabled=false;claude.Enabled=codex.Enabled=consent.Enabled=replace.Enabled=false;
            try {
                if(new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))throw new InvalidOperationException("請關閉此程式，改以一般員工身分直接雙擊開啟。使用其他管理員帳號會將設定存到錯誤帳號。");
                status.Text="正在核對安裝包、公司憑證與撤銷狀態…";
                string tools=claude.Checked?(codex.Checked?"Both":"Claude"):"Codex";bool switchProvider=replace.Checked;timer.Start();
                int exit=await Task.Run(()=> {
                    directory=Package.Extract();Package.InstallUserTrust(directory);
                    string powershell=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"System32","WindowsPowerShell","v1.0","powershell.exe");
                    string arguments="-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File \""+Path.Combine(directory,"Setup-CompanyAI.ps1")+"\" -Tools "+tools+(switchProvider?" -ReplaceProviderConfig":"")+
                        " -InstallerStatusPath \""+Path.Combine(directory,"status.json")+"\" -InstallerCancelPath \""+Path.Combine(directory,"cancel.request")+"\"";
                    var info=new ProcessStartInfo(powershell,arguments) {UseShellExecute=false,WorkingDirectory=directory,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=System.Text.Encoding.UTF8,StandardErrorEncoding=System.Text.Encoding.UTF8};
                    using(var child=new Process {StartInfo=info}) {
                        DataReceivedEventHandler output=(s,e)=>{if(e.Data!=null)BeginInvoke((Action)(()=>{if(progress.TextLength>100000)progress.Clear();progress.AppendText(e.Data+Environment.NewLine);}));};
                        child.OutputDataReceived+=output;child.ErrorDataReceived+=output;child.Start();child.BeginOutputReadLine();child.BeginErrorReadLine();child.WaitForExit();return child.ExitCode;
                    }
                });
                ReadStatus();
                if(exit!=0)throw new InvalidOperationException(FailureMessage(failureCode)+(!string.IsNullOrEmpty(currentStage)?"\n失敗階段："+currentStage:""));
                status.Text="設定完成。請完整關閉所有 Windows Terminal／PowerShell 視窗，再重新開啟並輸入 claude 或 codex。";
                MessageBox.Show(this,status.Text,"changdar-ai",MessageBoxButtons.OK,MessageBoxIcon.Information);
            } catch(Exception error) {status.Text=error.Message;if(failureCode!="CANCELLED")MessageBox.Show(this,error.Message,"changdar-ai 設定未完成",MessageBoxButtons.OK,MessageBoxIcon.Error);}
            finally {
                timer.Stop();running=false;cancel.Enabled=browser.Enabled=copy.Enabled=false;approvalUrl=null;approval.Text="";
                claude.Enabled=codex.Enabled=consent.Enabled=replace.Enabled=true;start.Text="重新設定";start.Enabled=consent.Checked&&(claude.Checked||codex.Checked);
            }
        }
    }

    internal static class Program {
        [STAThread] private static int Main(string[] args) {
            try {
                if(args.Length==2 && args[0]=="--self-test") {
                    Package.ValidateResources();string directory=Package.Extract();Package.VerifyAll(directory);
                    File.WriteAllText(args[1],"{\"product\":\"changdar-ai\",\"resources_valid\":true,\"native_signatures_valid\":true,\"trust_changed\":false,\"enrollment_created\":false}");return 0;
                }
                if(args.Length==2 && args[0]=="--ui-self-test") {Application.EnableVisualStyles();SetupForm.TestUI(args[1]);return 0;}
                if(args.Length!=0) throw new InvalidOperationException("不支援的啟動參數。");
                bool created;
                using(var mutex=new System.Threading.Mutex(true,"Local\\ChangdarAI.Setup."+WindowsIdentity.GetCurrent().User.Value,out created)) {
                    if(!created)throw new InvalidOperationException("changdar-ai 設定工具已開啟。請回到原本的設定視窗，避免重複綁定。");
                    try {Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new SetupForm());return 0;}
                    finally {mutex.ReleaseMutex();}
                }
            } catch(Exception error) {
                if(args.Length==2 && (args[0]=="--self-test"||args[0]=="--ui-self-test")) File.WriteAllText(args[1],new JavaScriptSerializer().Serialize(new{error=error.Message}));
                else MessageBox.Show(error.Message,"changdar-ai",MessageBoxButtons.OK,MessageBoxIcon.Error);
                return 1;
            }
        }
    }
}
