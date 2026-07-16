import PyPDF2
import pdfplumber
from pathlib import Path
from src.utils.file_utils import FileUtils

class PDFProcessor:
    """PDF文件处理类"""
    
    def __init__(self):
        """初始化PDF处理器"""
        pass
    
    def extract_text(self, pdf_path, pbar=None):
        """
        从PDF文件中提取文本
        
        Args:
            pdf_path: PDF文件路径
            pbar: tqdm进度条对象，用于输出信息
            
        Returns:
            提取的文本内容
        """
        FileUtils.write_message(f"开始提取PDF文本: {pdf_path}", pbar)
        try:
            text = ""
            
            # 尝试使用PyMuPDF提取文本
            FileUtils.write_message("尝试使用PyMuPDF提取文本...", pbar)
            try:
                import fitz
                doc = fitz.open(pdf_path)
                for page_num in range(doc.page_count):
                    page = doc[page_num]
                    page_text = page.get_text()
                    if page_text:
                        text += page_text + "\n"
                    FileUtils.write_message(f"第 {page_num + 1} 页提取成功", pbar)
                doc.close()
                FileUtils.write_message(f"PyMuPDF提取完成，总长度: {len(text)} 字符", pbar)
            except Exception as e:
                FileUtils.write_message(f"PyMuPDF提取失败: {e}", pbar)
                
                # 如果PyMuPDF提取失败，尝试使用pdfplumber
                FileUtils.write_message("尝试使用pdfplumber提取文本...", pbar)
                with pdfplumber.open(pdf_path) as pdf:
                    FileUtils.write_message(f"PDF页数: {len(pdf.pages)}", pbar)
                    for i, page in enumerate(pdf.pages):
                        page_text = page.extract_text()
                        if page_text:
                            text += page_text + "\n"
                            FileUtils.write_message(f"第 {i+1} 页提取成功", pbar)
                        else:
                            FileUtils.write_message(f"第 {i+1} 页提取失败", pbar)
                
                # 如果pdfplumber提取失败，尝试使用PyPDF2
                if not text:
                    FileUtils.write_message("pdfplumber提取失败，尝试使用PyPDF2...", pbar)
                    with open(pdf_path, 'rb') as file:
                        reader = PyPDF2.PdfReader(file)
                        FileUtils.write_message(f"PDF页数: {len(reader.pages)}", pbar)
                        for i in range(len(reader.pages)):
                            page = reader.pages[i]
                            page_text = page.extract_text()
                            if page_text:
                                text += page_text + "\n"
                                FileUtils.write_message(f"第 {i+1} 页提取成功", pbar)
                            else:
                                FileUtils.write_message(f"第 {i+1} 页提取失败", pbar)
            
            FileUtils.write_message(f"文本提取完成，总长度: {len(text)} 字符", pbar)
            return text
        except Exception as e:
            FileUtils.write_message(f"提取PDF文本失败: {e}", pbar)
            import traceback
            traceback.print_exc()
            return ""
    
    def get_pdf_files(self, directory):
        """
        获取目录中的所有PDF文件
        
        Args:
            directory: 目录路径
            
        Returns:
            PDF文件路径列表
        """
        pdf_files = []
        directory_path = Path(directory)
        
        if directory_path.exists() and directory_path.is_dir():
            for file_path in directory_path.glob("**/*.pdf"):
                pdf_files.append(str(file_path))
        
        return pdf_files
