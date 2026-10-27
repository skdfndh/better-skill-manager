# OCR 本地失败停止补丁

[pdf-ocr-stop-implicit-cloud.patch](pdf-ocr-stop-implicit-cloud.patch) 只修改 `PDFOCRProcessor._init_engine` 中的失败处理：RapidOCR 初始化失败时抛出异常，保留本地引擎状态，不自动构造外部云端引擎。显式选择 `siliconflow` 的路径保留。

补丁针对本轮检查的 pdf-ocr-skill 本地 2.5.0 来源文件。不同来源或后续版本可能不适用，先预览差异并运行 `git apply --check`。在原技能目录使用补丁的实际绝对路径：

```text
git apply --check <本仓库>/patches/pdf-ocr-stop-implicit-cloud.patch
git apply <本仓库>/patches/pdf-ocr-stop-implicit-cloud.patch
```

应用后应记录本地改造，防止上游更新覆盖。`scripts/pdf_ocr_processor.py` 原有许可证与来源继续适用，[上游 MIT 许可](pdf-ocr-LICENSE.txt)随补丁保留。仓库未附带原包全文；本补丁不安装依赖，也不改变所有引擎的兼容性。

## 离线验证

提供明确的原版源文件即可在临时目录中检查和应用补丁，并用替身验证真实初始化方法；不会改写传入的文件：

```text
python experiments/skill-curation/verify_ocr_patch.py <原版的pdf_ocr_processor.py>
```

已修改的本机文件可额外作为第二个参数核对结果。脚本只提取初始化方法，不导入整个 OCR 模块，不联网或安装依赖。实际文字识别、模型下载和第三方 API 仍需另行验证，不能以初始化替身测试代替。

验证脚本先核对已审阅源文件的规范化 SHA-256，避免执行其他版本或未知文件中的初始化方法；补丁结果也绑定哈希。源文件不匹配时会停止，不能通过忽略失败来宣称已验证新版本。
