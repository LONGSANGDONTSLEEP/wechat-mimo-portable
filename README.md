# 微信分身 Studio 0.5.0

把WeTrace导出、聊天记录库、本人风格、联系人记忆、回复试验室和微信OCR自动回复放进同一个Windows桌面工作台。

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
这是本地风格提取 + 本人例句 + 联系人记忆检索，生成时调用MiMo，不是对MiMo底层参数做微调，也不是万能模型。
默认更主动地回应和自然追问；不会定时向其他联系人主动发消息。
自动回复只作用于一个锁定窗口的当前聊天，不遍历好友、不自动切换会话。
只有成功发送的AI对话会加入该联系人的近期上下文；AI生成的内容不会反过来当作本人的训练样本。

## 数据与便携
记录库位于data/persona.sqlite3，配置位于config.json。记录、导出文件和Key不会打进公开发布包。
原始记录留在本机。点击生成或自动回复时，会把少量本人例句、选中联系人的记忆、相关历史片段和当前消息发送给配置的MiMo服务；常见手机号/邮箱/Key在这些片段中会做遮蔽，但不是完整脱敏保证。
使用“查看本次使用的提示与记忆”可检查提交内容。
便携包自带离线OCR模型和WeTrace公共运行文件。第一次启动会解压运行库，较旧版慢。
纯净Windows如WeTrace提示缺少VC运行库，可手动运行vendor/wetrace/Install-VCRuntime.cmd；不会自动安装。
WeTrace首次运行需要在其界面加载你有权使用的数据；此App不会自动提取微信密钥或解密数据。

## 组件与许可证
本项目桌面工作台代码MIT授权。
WeTrace独立组件来自用户提供的wetrace-portable-master包，原来源https://github.com/afumu/wetrace，沿用CC BY-NC-SA 4.0，仅非商业用途，保留原作者署名和许可证；wx_key沿用MIT。
原始WeTrace说明与许可证在vendor/wetrace中，离线OCR等第三方许可证在THIRD_PARTY_LICENSES。

## 验证
30项自动测试涵盖真实WeTrace字段结构、本人筛选、导入去重、联系人隔离、风格到模型请求的完整数据路径、OCR气泡和发送拦截。
WeTrace组件启动和本机HTTP页面测试通过。
独立EXE执行自建演示语料、多个页面、窗口截图和离线OCR自检。
尚未获得实际导出样例，真实历史文件兼容性和真实MiMo/微信端到端收发仍需在本机导入和预览验证。

## 开发
Python3.13 x64；pip install -r requirements.txt。
运行python -m unittest discover -s tests；运行build.ps1生成WeChatStudio.exe。
WeTrace二进制不提交到源码仓库；发行ZIP单独附带公共组件文件，不附带其data目录。
