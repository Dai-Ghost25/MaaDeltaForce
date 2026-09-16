# MaaDeltaForce
一个基于MaaFramework开发的简单挂机脚本

本项目仅作为学习Maa自动化识别框架的个人项目，根据自身所需设计，大概率无法直接使用，需要根据自身做出修改，这里仅作为参考，直接运行导致损失概不负责

Maa自动化识别框架
MaaFramework：https://github.com/MaaXYZ/MaaFramework

Maa调试工具
MaaDebugger：https://github.com/MaaXYZ/MaaDebugger

如何启动

列出任务
python cli.py list

执行单个任务
python cli.py run 邮件检查

按顺序执行多个任务
python cli.py run 启动任务 邮件检查 结束任务

定时任务：
编辑schedules.yaml文件，写入需要执行的任务和时间
启动它：
python mdf.py schedule daemon

如何自己编辑？
assets/resource下存放资源文件，其中
image存放图片
model存放模型（已导入OCR模型）
pipeline存放任务
具体任务流水线参考Maa的文档：
https://maafw.com/docs/3.1-PipelineProtocol
或者在项目中打开：
deps\docs\zh_cn\3.1-任务流水线协议.md
（是的，下载后我没有删掉他的文档）
调试过程中，强烈建议直接使用MaaDebugger

编辑完任务json文件后，在core下编辑config.py文件，将任务注册到系统中

你可能需要注意
1、MaaFramework目标分辨率为1280x720，如果分辨率不对应，将短边转换到720来处理
  例如：2560x1600分辨率的设备直接使用，在maaFramework眼中是1152x720
2、使用MaaDebugger的过程中，通过OCR或TemplateMatch识别后，可以在任务节点中看到box，你可以根据box来添加roi，这样降低识别范围，提高效率
3、本项目还有很多代码要写、bug要改、文档要敲，有时间我会加上。。。