# 微信分身 Studio 0.5.1

把WeTrace导出、聊天记录库、本人风格、联系人记忆、回复试验室和微信OCR自动回复放进同一个Windows桌面工作台。

## 使用本地Ollama
在“模型与工具”将服务切换为“本机Ollama”。默认地址http://127.0.0.1:11434/v1。
先启动你本机的Ollama，再点击“读取Ollama本地模型列表”，选择已下载的模型，点“测试模型连接”，最后保存配置。
本机Ollama不需要Key，原MiMo Key不会发送给Ollama。本地请求绕过系统HTTP代理，不会自动回退到MiMo。
风格、例句、联系人记忆及上下文照常使用，但提交给选择的本地模型。请选实际下载的本地模型，不要用cloud模型标签来期待完全离线。
首次加载模型可能较慢，本地请求允许最多300秒等待；停止后会阻止后续微信发送。
不附带Ollama程序或大语言模型，不自动安装或下载。只附带本App的离线OCR模型。
识别为云模型的条目不列入本地模型列表；可手动输入自己已安装模型的准确名称。
官方接口：https://docs.ollama.com/api/openai-compatibility
模型列表：https://docs.ollama.com/api/tags

## 第一次使用
1. 解压完整便携ZIP，双击WeChatStudio.exe。无需安装Python。
2. 在“模型与工具”填写MiMo Key，选择按量付费或Token Plan，测试连接。Key不写入磁盘。
3. 已有导出文件：在“聊天记录”导入JSON/CSV或WeTrace HTML备份文件夹。
4. 尚未导出：点击“启动WeTrace导出工具”。组件在独立工具窗口显示原有WeTrace界面；没有Edge时使用默认浏览器。导出后回到记录库导入。
5. 预览导入字段。isSelf/IsSender为本人标记；没有标记时填写自己在导出中的准确称呼。未知身份不参与学习。
6. “我的分身”查看本地提取的口头禅、典型长度和本人例句，调整更充分回应、主动接话选项。
7. “联系人记忆”选择联系人并添加已确认事实。不同联系人记忆隔离。
8. 先在“回复试验室”生成回复，不会发送微信；可查看本次使用的提示、风格和记忆。
9. 确认效果后在“微信自动回复”选择窗口、确认输入位置，默认先预览。Esc或鼠标移到主屏幕左上角可停止。

## 文件兼容
已按用户提供的WeTrace便携程序中的真实HTML格式适配window.CHAT_DATA（isSelf、senderName、talkerName、createTime、content、type）。
支持JSON消息列表、messages/Items/data外层对象、JSONL、CSV/TSV及带时间发送者的TXT。
HTML仅提取结构化JSON，不执行脚本、不访问远程媒体。只导入文本，语音/图片/系统消息不作为风格语料。
不同导出版本可在预览里映射字段；无法识别时明确报错，不猜测谁是本人。

## 这次“学习”的含义
这是本地风格提取 + 本人例句 + 联系人记忆检索，生成时调用选定的云端或本地模型，不是对MiMo底层参数做微调，也不是万能模型。
默认更主动地回应和自然追问；不会定时向其他联系人主动发消息。
自动回复只作用于一个锁定窗口的当前聊天，不遍历好友、不自动切换会话。
只有成功发送的AI对话会加入该联系人的近期上下文；AI生成的内容不会反过来当作本人的训练样本。

## 数据与便携
记录库位于data/persona.sqlite3，配置位于config.json。记录、导出文件和Key不会打进公开发布包。
原始记录留在本机。点击生成或自动回复时，会把少量本人例句、选中联系人的记忆、相关历史片段和当前消息发送给所选模型服务（本地模式提交到本地服务）；常见手机号/邮箱/Key在这些片段中会做遮蔽，但不是完整脱敏保证。
使用“查看本次使用的提示与记忆”可检查提交内容。
便携包自带离线OCR模型和WeTrace公共运行文件。第一次启动会解压运行库，较旧版慢。
纯净Windows如WeTrace提示缺少VC运行库，可手动运行vendor/wetrace/Install-VCRuntime.cmd；不会自动安装。
WeTrace首次运行需要在其界面加载你有权使用的数据；此App不会自动提取微信密钥或解密数据。

## 组件与许可证
本项目桌面工作台代码MIT授权。
WeTrace独立组件来自用户提供的wetrace-portable-master包，原来源https://github.com/afumu/wetrace，沿用CC BY-NC-SA 4.0，仅非商业用途，保留原作者署名和许可证；wx_key沿用MIT。
原始WeTrace说明与许可证在vendor/wetrace中，离线OCR等第三方许可证在THIRD_PARTY_LICENSES。

## 验证
33项自动测试涵盖真实WeTrace字段结构、本人筛选、导入去重、联系人隔离、风格到模型请求的完整数据路径、OCR气泡和发送拦截。
WeTrace组件启动和本机HTTP页面测试通过。
独立EXE执行自建演示语料、多个页面、窗口截图和离线OCR自检。
尚未获得实际导出样例，真实历史文件兼容性和真实MiMo/微信端到端收发仍需在本机导入和预览验证。

## 开发
Python3.13 x64；pip install -r requirements.txt。
运行python -m unittest discover -s tests；运行build.ps1生成WeChatStudio.exe。
WeTrace二进制不提交到源码仓库；发行ZIP单独附带公共组件文件，不附带其data目录。

## LM Studio 本地模型
在 LM Studio 下载并加载聊天模型，打开 Developer 中的本地服务器（默认端口1234）。
本 App 的模型与工具选择“本机 LM Studio”，地址 http://127.0.0.1:1234/v1，点击读取本地模型列表，选择模型后测试连接。默认无鉴权服务不填 Key；本地模式不转发 MiMo Key。
推荐16GB显存从Qwen3-14B Q4_K_M、4096上下文起步，按实际显存占用调整。
LM Studio 和语言模型需另行下载，便携包不捆绑大模型。官方服务说明：https://lmstudio.ai/docs/developer/core/server
