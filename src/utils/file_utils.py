from pathlib import Path

class FileUtils:
    """文件工具类"""
    
    @staticmethod
    def get_output_path(input_path, output_dir=None):
        """
        根据输入文件路径生成输出文件路径
        
        Args:
            input_path: 输入文件路径
            output_dir: 输出目录
            
        Returns:
            输出文件路径
        """
        input_path = Path(input_path)
        
        # 如果指定了输出目录
        if output_dir:
            output_dir_path = Path(output_dir)
            output_dir_path.mkdir(parents=True, exist_ok=True)
            output_filename = input_path.stem + ".md"
            output_path = output_dir_path / output_filename
        else:
            # 默认输出到同一目录
            output_filename = input_path.stem + ".md"
            output_path = input_path.parent / output_filename
        
        # 处理边界问题：如果存在同名文件，添加"-新增"后缀
        counter = 1
        original_output_path = output_path
        while output_path.exists():
            if counter == 1:
                output_filename = f"{input_path.stem}-新增.md"
            else:
                output_filename = f"{input_path.stem}-新增{counter}.md"
            
            if output_dir:
                output_path = output_dir_path / output_filename
            else:
                output_path = input_path.parent / output_filename
            counter += 1
        
        return str(output_path)
    
    @staticmethod
    def write_text(text, output_path):
        """
        将文本写入文件
        
        Args:
            text: 文本内容
            output_path: 输出文件路径
            
        Returns:
            是否写入成功
        """
        try:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(text)
            return True
        except Exception as e:
            print(f"写入文件失败: {e}")
            return False
    
    @staticmethod
    def write_message(message, pbar=None):
        """
        输出消息，根据是否存在pbar决定输出方式
        
        Args:
            message: 要输出的消息
            pbar: tqdm进度条对象，用于输出信息
        """
        if pbar:
            pbar.write(message)
        else:
            print(message)
