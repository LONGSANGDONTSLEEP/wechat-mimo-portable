# 微信 MiMo 助手 0.2.0（Windows x64 便携版）

全窗口 OCR，无需选窗口、框选区域或坐标校准。
自动定位微信主窗口，截图识别聊天标题、左侧消息、未读红点、聊天列表与输入框边界，模拟键盘粘贴发送。
不使用微信API、数据库解密或UI Automation。

## 使用
1. 解压ZIP，双击WeChatMiMo.exe。无需安装Python。
2. 打开并登录一个微信，显示主聊天界面并打开任意聊天。窗口完整显示，至少650×450。
3. 填入MiMo Key，选择按量付费或Token Plan；模型和接口地址可改。
4. “仅回复此聊天”可留空，自动点击可见未读会话；填写标题则仅回复该聊天。
5. 点击开始。自动激活微信并识别全窗口，忽略启动时当前聊天旧消息。其他会话的可见未读消息也会处理。
6. 默认生成预览，点“发送当前预览”发送。确认正确后停止，勾选自动发送，再开始。
7. Esc全局停止；鼠标移至主屏幕左上角也会停止。

配置保存到程序旁边，API Key仅保留在进程中，不写入配置；可通过MIMO_API_KEY环境变量提供。

## 范围和限制
Windows 10/11，需系统已安装简体中文OCR功能。标准浅色聊天布局，深色/皮肤/异常布局暂未验证。
只识别列表中可见的未读会话，不滚动遍历全部联系人。自动激活微信占用前台，请避免同时操作微信。
识别不到标题或输入框边界时停止，不猜测发送坐标。窗口移动后重新读取位置，不需要校准。
只处理文字；可见对方消息整体作为输入，不是精确气泡增量解析。内存中按聊天保留最多6轮历史。
标题、未读红点、消息左右方向均从图像推断，可能误识别，务必先用预览模式。
发送前检查聊天、消息及草稿，变化时停止；已有草稿不会自动清空。
回复提交到所选MiMo接口。API最多等待90秒；Esc阻止后续发送，但不取消已发起请求。
不保证零封号风险。真实MiMo调用与真实微信端到端兼容性尚未验证。

## 开发
Python 3.13 x64，安装requirements.txt。
测试：python -m unittest discover -s tests
打包：python -m PyInstaller --onefile --windowed --name WeChatMiMo --collect-all winocr --collect-all winrt app.py
独立自检：WeChatMiMo.exe --self-test，检查完整GUI初始化和真实中文OCR，结果写入self-test.txt，不发送消息。
MiMo文档：https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call
本项目为独立实现，MIT授权。
