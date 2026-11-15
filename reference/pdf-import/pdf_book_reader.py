#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
绘本PDF解析模块
功能：将绘本PDF逐页解析为结构化数据，包括：
  - 每页文字内容（OCR）
  - 每页画面描述（场景理解）
  - 每页可交互物体及坐标（目标检测）
  - 角色设定卡（全本角色汇总）
  - 整体美术风格总结
"""

import os
import json
import base64
import time
from typing import Dict, List, Optional, Tuple
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
    print("⚠️  警告：未安装 PyMuPDF，PDF转图片功能不可用。请运行: pip install pymupdf")

from PIL import Image
import io

from gemini_analyze import ImageObjectAnalyzer
from text_agent_base import Gemini3ProAgent


# ============================================================
# 工具函数
# ============================================================

def image_to_base64(image_path: str) -> str:
    """图片转base64"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode('utf-8')


def get_image_mime_type(image_path: str) -> str:
    ext = os.path.splitext(image_path)[1].lower()
    return {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
    }.get(ext, 'image/jpeg')


def pil_image_to_base64(pil_image: Image.Image, fmt: str = "JPEG") -> str:
    """PIL Image转base64"""
    buf = io.BytesIO()
    pil_image.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def clean_json_response(content: str) -> Optional[dict]:
    """从LLM响应中提取并解析JSON"""
    cleaned = content.strip()
    # 尝试提取 ```json ... ``` 块
    if '```json' in cleaned:
        start = cleaned.find('```json') + 7
        end = cleaned.find('```', start)
        if end > start:
            cleaned = cleaned[start:end].strip()
    elif '```' in cleaned:
        start = cleaned.find('```') + 3
        end = cleaned.find('```', start)
        if end > start:
            cleaned = cleaned[start:end].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 尝试修复常见问题
        try:
            # 去掉末尾可能多余的逗号
            cleaned_fixed = cleaned.rstrip().rstrip(',')
            return json.loads(cleaned_fixed)
        except Exception:
            print(f"❌ JSON解析失败，原始内容：\n{content[:500]}")
            return None


# ============================================================
# PDF绘本解析器
# ============================================================

class PDFBookReader:
    """
    绘本PDF解析器
    
    输入：一本绘本的PDF文件
    输出：结构化的绘本数据，包含文字、画面描述、物体检测坐标、角色信息等
    """
    
    def __init__(self, 
                 vl_base_url: str,
                 vl_api_key: str,
                 vl_model: str,
                 llm_base_url: str = None,
                 llm_api_key: str = None,
                 llm_model: str = None,
                 dpi: int = 200):
        """
        Args:
            vl_base_url: 多模态VL模型API地址（用于OCR和画面理解）
            vl_api_key: VL模型API密钥
            vl_model: VL模型名称
            llm_base_url: 文本LLM API地址（用于汇总和角色提取，如不填则复用VL）
            llm_api_key: 文本LLM API密钥
            llm_model: 文本LLM模型名称
            dpi: PDF转图片的分辨率，默认200（兼顾清晰度和速度）
        """
        self.vl_base_url = vl_base_url
        self.vl_api_key = vl_api_key
        self.vl_model = vl_model
        self.dpi = dpi
        
        # 如果没有单独指定LLM，就复用VL模型的配置（假设VL也能做文本任务）
        self.llm_base_url = llm_base_url or vl_base_url
        self.llm_api_key = llm_api_key or vl_api_key
        self.llm_model = llm_model or vl_model
        
        # 物体检测器（复用现有模块）
        self.object_analyzer = ImageObjectAnalyzer(
            base_url=vl_base_url,
            api_key=vl_api_key,
            model=vl_model
        )
        
        # 进度回调
        self._progress_callback = None
    
    def set_progress_callback(self, callback):
        """设置进度回调函数 callback(stage, current, total, message)"""
        self._progress_callback = callback
    
    def _report_progress(self, stage: str, current: int, total: int, message: str = ""):
        if self._progress_callback:
            try:
                self._progress_callback(stage, current, total, message)
            except Exception as e:
                print(f"进度回调异常: {e}")
    
    # --------------------------------------------------------
    # 步骤1：PDF转图片
    # --------------------------------------------------------
    
    def pdf_to_images(self, pdf_path: str, output_dir: str) -> Tuple[List[str], List[str]]:
        """
        将PDF逐页转为图片，同时提取每页的文字（兜底用）
        
        Args:
            pdf_path: PDF文件路径
            output_dir: 图片输出目录
            
        Returns:
            (图片路径列表, 每页提取的文本列表)
        """
        if fitz is None:
            raise RuntimeError("PyMuPDF 未安装，无法解析PDF。请运行: pip install pymupdf")
        
        os.makedirs(output_dir, exist_ok=True)
        
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        image_paths = []
        page_texts_pdf = []  # 从PDF直接提取的文字
        
        print(f"📄 PDF共 {total_pages} 页，开始转图片 (DPI={self.dpi})...")
        self._report_progress("pdf_to_image", 0, total_pages, "开始转换PDF为图片")
        
        zoom = self.dpi / 72  # 72是PDF默认DPI
        mat = fitz.Matrix(zoom, zoom)
        
        for page_num in range(total_pages):
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)
            image_path = os.path.join(output_dir, f"page_{page_num + 1:03d}.jpg")
            pix.save(image_path)
            image_paths.append(image_path)
            
            # 同时提取PDF内置文字（兜底用）
            try:
                text = page.get_text().strip()
            except Exception:
                text = ""
            page_texts_pdf.append(text)
            
            self._report_progress("pdf_to_image", page_num + 1, total_pages, 
                                 f"第 {page_num + 1}/{total_pages} 页")
        
        doc.close()
        
        # 统计有多少页有文字
        pages_with_text = sum(1 for t in page_texts_pdf if t.strip())
        print(f"✅ PDF转图片完成，共 {len(image_paths)} 页，其中 {pages_with_text} 页有内置文字")
        return image_paths, page_texts_pdf
    
    # --------------------------------------------------------
    # 步骤2：单页OCR文字识别
    # --------------------------------------------------------
    
    def ocr_page(self, image_path: str) -> str:
        """
        用多模态模型识别单页文字内容
        
        Args:
            image_path: 页面图片路径
            
        Returns:
            识别出的文字内容
        """
        import requests
        
        base64_image = image_to_base64(image_path)
        mime_type = get_image_mime_type(image_path)
        
        system_prompt = """你是一个专业的绘本OCR识别助手，擅长识别儿童绘本中的文字内容。
        要求：
        1. 严格逐字逐句识别页面中的所有文字，包括标题、正文、页码
        2. 保持原文的段落结构和标点符号
        3. 如果页面没有文字，输出"[无文字页面]"
        4. 不要添加任何解释、说明或你的思考过程，只输出识别到的文字
        5. 注意区分绘本中的文字和画面中的装饰性文字
        6. 绘本的正文和标题要完整识别
        """
        
        prompt = """请识别这张绘本页面中的所有文字内容。
        要求：
        1. 逐字逐句准确识别
        2. 保持原文顺序和标点
        3. 如果有标题，请单独标注
        4. 只输出文字内容，不要其他说明"""
        
        payload = json.dumps({
            "model": self.vl_model,
            "temperature": 0.1,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                    }
                ]}
            ]
        })
        
        try:
            response = requests.post(
                self.vl_base_url,
                headers={
                    "Authorization": f"Bearer {self.vl_api_key}",
                    "Content-Type": "application/json"
                },
                data=payload,
                timeout=120
            )
            result = response.json()
            content = result['choices'][0]['message']['content']
            return content.strip()
        except Exception as e:
            print(f"❌ OCR识别失败: {e}")
            return ""
    
    # --------------------------------------------------------
    # 步骤3：单页画面理解
    # --------------------------------------------------------
    
    def analyze_page_scene(self, image_path: str) -> Dict:
        """
        分析单页画面内容（场景、角色、动作、氛围、风格）
        
        Args:
            image_path: 页面图片路径
            
        Returns:
            画面分析结果字典
        """
        import requests
        
        base64_image = image_to_base64(image_path)
        mime_type = get_image_mime_type(image_path)
        
        system_prompt = """你是一个专业的绘本画面分析专家，擅长分析儿童绘本的视觉内容。
        请用JSON格式输出分析结果。"""
        
        prompt = """请分析这张绘本页面的画面内容，以JSON格式返回。

        需要分析以下内容：
        {
            "scene": "场景地点，如森林、家里、学校等",
            "time_of_day": "时间，如白天、晚上、清晨等",
            "weather": "天气，如晴天、雨天、下雪等",
            "characters": [
                {
                    "name": "角色名称（如果能识别出名字就写名字，不能就写特征描述，如'穿红裙子的小兔子'）",
                    "appearance": "外观描述，包括颜色、服装、体型等",
                    "action": "这个角色在做什么动作",
                    "emotion": "角色的情绪状态"
                }
            ],
            "key_objects": ["画面中的关键物品列表，如桌子、花朵、城堡等"],
            "atmosphere": "整体氛围，如温馨、紧张、快乐、神秘等",
            "art_style": "美术风格，如水彩、扁平、吉卜力、卡通、写实等",
            "color_tone": "整体色调，如暖色调、冷色调、明亮、柔和等",
            "summary": "一句话总结这个画面讲了什么"
        }

        请严格按照JSON格式输出，不要有多余的文字说明。"""
        
        payload = json.dumps({
            "model": self.vl_model,
            "temperature": 0.3,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                    }
                ]}
            ]
        })
        
        try:
            response = requests.post(
                self.vl_base_url,
                headers={
                    "Authorization": f"Bearer {self.vl_api_key}",
                    "Content-Type": "application/json"
                },
                data=payload,
                timeout=120
            )
            result = response.json()
            content = result['choices'][0]['message']['content']
            parsed = clean_json_response(content)
            if parsed:
                return parsed
            else:
                return {"summary": content, "scene": "", "characters": [], "key_objects": []}
        except Exception as e:
            print(f"❌ 画面分析失败: {e}")
            return {"summary": "", "scene": "", "characters": [], "key_objects": []}
    
    # --------------------------------------------------------
    # 步骤4：单页物体检测（复用gemini_analyze）
    # --------------------------------------------------------
    
    def detect_objects(self, image_path: str, max_objects: int = 8) -> Dict[str, List[int]]:
        """
        检测页面中的可交互物体及边界框坐标
        
        Args:
            image_path: 页面图片路径
            max_objects: 最多检测的物体数量
            
        Returns:
            {"物体名称<English>": [x1, y1, x2, y2], ...}
        """
        try:
            result = self.object_analyzer.call({
                "image_path": image_path,
                "max_objects": max_objects,
                "debug": False
            })
            if isinstance(result, dict) and "error" not in result:
                return result
            else:
                print(f"⚠️  物体检测结果异常: {result}")
                return {}
        except Exception as e:
            print(f"❌ 物体检测失败: {e}")
            return {}
    
    def _simple_scene_guess(self, text: str, page_num: int) -> Dict:
        """
        VL不可用时，从文字简单推断场景信息（兜底用）
        """
        import re
        
        # 从文字中提取一些关键词
        text = text.strip()
        if not text:
            return {"summary": "", "scene": "", "characters": [], "key_objects": []}
        
        # 简单的场景关键词匹配
        scene_keywords = {
            "森林": "森林", "树林": "森林", "树": "森林", "丛林": "森林",
            "家": "家里", "房子": "家里", "房间": "家里", "屋里": "家里",
            "学校": "学校", "幼儿园": "幼儿园", "教室": "学校",
            "天空": "天空", "云": "天空", "星星": "夜空", "月亮": "夜空",
            "海": "海边", "沙滩": "海边", "海洋": "海边",
            "山": "山上", "山顶": "山上",
            "河": "河边", "湖": "湖边",
            "农场": "农场", "乡村": "乡村",
            "城堡": "城堡", "王国": "城堡",
            "太空": "太空", "宇宙": "太空",
        }
        
        detected_scene = "绘本场景"
        for kw, scene in scene_keywords.items():
            if kw in text:
                detected_scene = scene
                break
        
        # 简单提取角色（找"小X"、"XX的XX"等模式）
        characters = []
        char_patterns = [
            r'小[\u4e00-\u9fa5]{1,3}',  # 小兔子、小猫咪
            r'[\u4e00-\u9fa5]{2,3}爷爷',  # 大树爷爷
            r'[\u4e00-\u9fa5]{2,3}奶奶',
            r'[\u4e00-\u9fa5]{2,3}妈妈',
            r'[\u4e00-\u9fa5]{2,3}爸爸',
        ]
        
        found_chars = set()
        for pattern in char_patterns:
            matches = re.findall(pattern, text)
            found_chars.update(matches)
        
        for char_name in list(found_chars)[:3]:
            characters.append({
                "name": char_name,
                "appearance": "",
                "action": "",
                "emotion": ""
            })
        
        return {
            "scene": detected_scene,
            "time_of_day": "白天",
            "weather": "晴天",
            "characters": characters,
            "key_objects": [],
            "atmosphere": "温馨",
            "art_style": "儿童绘本风格",
            "color_tone": "明亮",
            "summary": text[:50] + "..." if len(text) > 50 else text
        }
    
    def _extract_characters_fast(self, all_pages_data: List[Dict], page_texts: List[str]) -> Dict:
        """
        快速模式：从全本文字中用正则提取角色名（不调用LLM，秒出）
        
        Args:
            all_pages_data: 所有页面数据
            page_texts: 所有页面的文字内容
            
        Returns:
            角色设定字典（简化版）
        """
        import re
        
        # 合并全文
        full_text = "".join(page_texts)
        
        # 角色名匹配模式
        char_patterns = [
            r'小[\u4e00-\u9fa5]{1,2}',      # 小兔子、小熊
            r'[\u4e00-\u9fa5]{2}爷爷',       # 大树爷爷、山羊爷爷
            r'[\u4e00-\u9fa5]{2}奶奶',
            r'[\u4e00-\u9fa5]{2}妈妈',
            r'[\u4e00-\u9fa5]{2}爸爸',
            r'[\u4e00-\u9fa5]{2}姐姐',
            r'[\u4e00-\u9fa5]{2}哥哥',
            r'[\u4e00-\u9fa5]{2}弟弟',
            r'[\u4e00-\u9fa5]{2}妹妹',
        ]
        
        # 统计每个角色名出现的次数
        char_count = {}
        for pattern in char_patterns:
            matches = re.findall(pattern, full_text)
            for name in matches:
                # 过滤掉一些常见的误匹配
                if name in ["小心", "小时", "小孩", "小朋友", "小人", "大家"]:
                    continue
                char_count[name] = char_count.get(name, 0) + 1
        
        # 按出现次数排序，取前8个
        sorted_chars = sorted(char_count.items(), key=lambda x: x[1], reverse=True)[:8]
        
        characters = []
        for idx, (name, count) in enumerate(sorted_chars):
            characters.append({
                "name": name,
                "name_en": "",
                "appearance": f"绘本中的角色{name}",
                "colors": [],
                "personality": "",
                "importance": "main" if idx < 3 else "supporting"
            })
        
        return {
            "characters": characters,
            "art_style_summary": "儿童绘本风格",
            "color_palette": ["明亮", "温暖", "童趣"]
        }
    
    # --------------------------------------------------------
    # 步骤5：角色汇总（全本）
    # --------------------------------------------------------
    
    def extract_characters(self, all_pages_data: List[Dict]) -> Dict:
        """
        从所有页面数据中汇总角色信息，生成角色设定卡
        
        Args:
            all_pages_data: 所有页面的分析数据
            
        Returns:
            角色设定字典 {"角色名": {description, appearance, colors, ...}}
        """
        # 收集所有页面的角色信息
        all_characters_info = []
        for page in all_pages_data:
            scene = page.get("scene_analysis", {})
            chars = scene.get("characters", [])
            all_characters_info.extend(chars)
        
        # 用LLM汇总去重，生成统一的角色卡
        characters_json = json.dumps(all_characters_info, ensure_ascii=False, indent=2)
        
        system_prompt = """你是一个儿童绘本角色设定专家，需要从分散的角色描述中提取并整理出统一的角色设定卡。"""
        
        prompt = f"""以下是一本绘本中各页出现的角色描述（可能有重复和不一致）：
```json
{characters_json}
```

请整理出这本绘本的主要角色设定卡，要求：
1. 合并相同的角色（同一角色在不同页面的描述要统一）
2. 每个角色包含：名称、外观特征（颜色、服装、体型等）、性格特点
3. 按重要性排序，主要角色在前
4. 输出JSON格式：

```json
{{
  "characters": [
    {{
      "name": "角色中文名",
      "name_en": "角色英文名（如果原文有就写，没有可以翻译或留空）",
      "appearance": "详细的外观描述，用于AI生图参考",
      "colors": ["主色1", "主色2"],
      "personality": "性格特点",
      "importance": "main" 或 "supporting"
    }}
  ],
  "art_style_summary": "整体美术风格总结，用于指导AI生图",
  "color_palette": ["整体色调1", "整体色调2", "整体色调3"]
}}
```

请只输出JSON，不要其他文字。"""
        
        try:
            agent = Gemini3ProAgent(
                base_url=self.llm_base_url,
                api_key=self.llm_api_key,
                model=self.llm_model,
                system_prompt=system_prompt,
                track_history=False
            )
            response, success = agent.call(prompt, temperature=0.3, max_new_token=2048)
            if success:
                parsed = clean_json_response(response)
                if parsed:
                    return parsed
        except Exception as e:
            print(f"❌ 角色汇总失败: {e}")
        
        # 失败时返回基本结构（尽量用收集到的角色名填充）
        character_names = set()
        for page in all_pages_data:
            scene = page.get("scene_analysis", {})
            for char in scene.get("characters", []):
                if char.get("name"):
                    character_names.add(char["name"])
        
        characters = []
        for name in sorted(character_names):
            characters.append({
                "name": name,
                "name_en": "",
                "appearance": f"绘本中的角色{name}",
                "colors": [],
                "personality": "",
                "importance": "main" if len(characters) < 3 else "supporting"
            })
        
        return {
            "characters": characters,
            "art_style_summary": "儿童绘本风格",
            "color_palette": ["明亮", "温暖", "童趣"]
        }
    
    # --------------------------------------------------------
    # 步骤6：OCR结果校验（逐字核对）
    # --------------------------------------------------------
    
    def verify_ocr(self, image_path: str, ocr_text: str) -> Tuple[str, bool]:
        """
        校验OCR结果是否准确（赛题要求：逐字逐页识别并能核对原文）
        
        Args:
            image_path: 页面图片
            ocr_text: 第一次OCR的结果
            
        Returns:
            (最终文本, 是否通过校验)
        """
        # 简单策略：再识别一次，如果两次结果相似度高就认为准确
        # 也可以让VL模型自己对比图片和文本
        import requests
        
        base64_image = image_to_base64(image_path)
        mime_type = get_image_mime_type(image_path)
        
        system_prompt = """你是一个严谨的OCR校验员，需要核对图片中的文字和给定文本是否一致。"""
        
        prompt = f"""请核对这张图片和下面的文本是否一致：

待核对文本：
\"\"\"{ocr_text}\"\"\"

请回答：
1. 文本和图片内容是否一致？（一致/不一致）
2. 如果不一致，正确的文本应该是什么？
3. 输出格式：JSON
{{
    "is_consistent": true/false,
    "corrected_text": "修正后的完整文本",
    "differences": ["差异点1", "差异点2"]
}}"""
        
        payload = json.dumps({
            "model": self.vl_model,
            "temperature": 0.1,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                    }
                ]}
            ]
        })
        
        try:
            response = requests.post(
                self.vl_base_url,
                headers={
                    "Authorization": f"Bearer {self.vl_api_key}",
                    "Content-Type": "application/json"
                },
                data=payload,
                timeout=120
            )
            result = response.json()
            content = result['choices'][0]['message']['content']
            parsed = clean_json_response(content)
            if parsed:
                is_consistent = parsed.get("is_consistent", False)
                corrected = parsed.get("corrected_text", ocr_text)
                return corrected, is_consistent
        except Exception as e:
            print(f"❌ OCR校验失败: {e}")
        
        # 校验失败时返回原文本
        return ocr_text, False
    
    # --------------------------------------------------------
    # 主流程：完整解析一本绘本
    # --------------------------------------------------------
    
    def read_book(self, pdf_path: str, output_dir: str = None, 
                  mode: str = "fast",
                  skip_cover: bool = True,
                  verify_ocr: bool = True,
                  max_pages: int = None) -> Dict:
        """
        完整解析一本绘本PDF
        
        Args:
            pdf_path: PDF文件路径
            output_dir: 输出目录（存放页面图片和解析结果）
            mode: 解析模式
                - "fast": 快速模式，只用PDF内置文字，不调用AI（秒出，推荐默认）
                - "full": 完整模式，调用VL模型做OCR+画面分析+物体检测（质量高但慢）
            skip_cover: 是否跳过封面和封底（只解析正文页）
            verify_ocr: 是否进行OCR结果校验（仅full模式有效）
            max_pages: 最多解析的页数（用于测试，None表示全部）
            
        Returns:
            完整的绘本结构化数据
        """
        start_time = time.time()
        
        # 准备输出目录
        if output_dir is None:
            pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
            output_dir = os.path.join("output_dir", f"book_{pdf_name}_{int(time.time())}")
        
        pages_dir = os.path.join(output_dir, "pages")
        os.makedirs(pages_dir, exist_ok=True)
        
        book_title = os.path.splitext(os.path.basename(pdf_path))[0]
        
        use_ai = (mode == "full")  # 是否使用AI模型分析
        
        # ===== 步骤1：PDF转图片 =====
        print("=" * 60)
        print("📖 步骤1/5：PDF转图片")
        print("=" * 60)
        image_paths, page_texts_pdf = self.pdf_to_images(pdf_path, pages_dir)
        
        # 限制页数（测试用）
        if max_pages and max_pages < len(image_paths):
            print(f"⚠️  限制解析前 {max_pages} 页（测试模式）")
            image_paths = image_paths[:max_pages]
            page_texts_pdf = page_texts_pdf[:max_pages]
        
        total_pages = len(image_paths)
        
        # ===== 步骤2：逐页文字提取 =====
        print("\n" + "=" * 60)
        if use_ai:
            print("📝 步骤2/5：OCR文字识别（AI模式）")
        else:
            print("📝 步骤2/5：文字提取（快速模式）")
        print("=" * 60)
        self._report_progress("ocr", 0, total_pages, "开始文字提取")
        
        page_texts = []
        if use_ai:
            # 完整模式：用VL模型做OCR
            for i, img_path in enumerate(image_paths):
                print(f"  [{i+1}/{total_pages}] 识别第 {i+1} 页...")
                text = self.ocr_page(img_path)
                page_texts.append(text)
                print(f"      文字长度: {len(text)} 字")
                self._report_progress("ocr", i + 1, total_pages, f"第 {i+1}/{total_pages} 页OCR完成")
        else:
            # 快速模式：直接用PDF内置文字（秒出）
            print("  ⚡ 快速模式：使用PDF内置文字")
            for i in range(total_pages):
                text = page_texts_pdf[i] if i < len(page_texts_pdf) else ""
                page_texts.append(text)
                self._report_progress("ocr", i + 1, total_pages, f"第 {i+1}/{total_pages} 页文字提取完成")
        
        # ===== 步骤3：OCR校验（仅完整模式） =====
        if use_ai and verify_ocr:
            print("\n" + "=" * 60)
            print("🔍 步骤3/5：OCR结果校验")
            print("=" * 60)
            self._report_progress("verify_ocr", 0, total_pages, "开始校验OCR结果")
            
            verified_texts = []
            for i, (img_path, text) in enumerate(zip(image_paths, page_texts)):
                if not text or text == "[无文字页面]":
                    verified_texts.append(text)
                    continue
                print(f"  [{i+1}/{total_pages}] 校验第 {i+1} 页...")
                corrected, is_ok = self.verify_ocr(img_path, text)
                verified_texts.append(corrected)
                if not is_ok:
                    print(f"      ⚠️  OCR有差异，已修正")
                self._report_progress("verify_ocr", i + 1, total_pages, 
                                     f"第 {i+1}/{total_pages} 页校验完成")
            page_texts = verified_texts
        else:
            # 跳过校验步骤，直接标记完成
            print("\n⚠️  跳过OCR校验")
            self._report_progress("verify_ocr", total_pages, total_pages, "跳过")
        
        # ===== 步骤4：逐页画面分析 + 物体检测 =====
        print("\n" + "=" * 60)
        if use_ai:
            print("🎨 步骤4/5：画面分析与物体检测（AI模式）")
        else:
            print("🎨 步骤4/5：场景推断（快速模式）")
        print("=" * 60)
        self._report_progress("scene_analysis", 0, total_pages, "开始画面分析")
        
        pages_data = []
        for i, (img_path, text) in enumerate(zip(image_paths, page_texts)):
            print(f"  [{i+1}/{total_pages}] 分析第 {i+1} 页...")
            
            # 画面分析
            if use_ai:
                scene_analysis = self.analyze_page_scene(img_path)
            else:
                # 快速模式：从文字中简单推断场景（不调AI，秒出）
                scene_analysis = self._simple_scene_guess(text, i + 1)
            
            # 物体检测（仅完整模式+有文字时做）
            objects = {}
            if use_ai and text and text != "[无文字页面]":
                objects = self.detect_objects(img_path, max_objects=8)
            
            page_data = {
                "page_num": i + 1,
                "image_path": os.path.abspath(img_path),
                "text": text if text != "[无文字页面]" else "",
                "scene_analysis": scene_analysis,
                "objects": objects,
            }
            pages_data.append(page_data)
            
            obj_count = len(objects) if objects else 0
            print(f"      场景: {scene_analysis.get('scene', '未知')}, "
                  f"角色: {len(scene_analysis.get('characters', []))}个, "
                  f"可交互物体: {obj_count}个")
            
            self._report_progress("scene_analysis", i + 1, total_pages,
                                 f"第 {i+1}/{total_pages} 页分析完成")
        
        # ===== 步骤5：角色汇总与全书整理 =====
        print("\n" + "=" * 60)
        if use_ai:
            print("👤 步骤5/5：角色汇总（AI模式）")
        else:
            print("👤 步骤5/5：角色提取（快速模式）")
        print("=" * 60)
        self._report_progress("character_extract", 0, 1, "提取角色设定")
        
        if use_ai:
            character_info = self.extract_characters(pages_data)
        else:
            # 快速模式：从文字中提取角色名，不调用LLM（秒出）
            character_info = self._extract_characters_fast(pages_data, page_texts)
        
        # ===== 组装最终结果 =====
        elapsed = time.time() - start_time
        
        book_data = {
            "title": book_title,
            "total_pages": total_pages,
            "language": self._detect_language(page_texts),
            "art_style_summary": character_info.get("art_style_summary", "儿童绘本风格"),
            "color_palette": character_info.get("color_palette", []),
            "characters": character_info.get("characters", []),
            "pages": pages_data,
            "generation_info": {
                "total_time_seconds": round(elapsed, 1),
                "pages_per_minute": round(total_pages / (elapsed / 60), 1) if elapsed > 0 else 0,
                "source_pdf": os.path.abspath(pdf_path),
                "output_dir": os.path.abspath(output_dir),
            }
        }
        
        # 保存结果
        result_path = os.path.join(output_dir, "book_data.json")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(book_data, f, ensure_ascii=False, indent=2)
        
        print(f"\n✅ 绘本解析完成！")
        print(f"   总页数: {total_pages}")
        print(f"   角色数: {len(book_data['characters'])}")
        print(f"   总耗时: {elapsed:.1f} 秒 ({elapsed/60:.1f} 分钟)")
        print(f"   结果已保存: {result_path}")
        
        self._report_progress("complete", 1, 1, f"解析完成，共{total_pages}页，耗时{elapsed:.1f}秒")
        
        return book_data
    
    def _detect_language(self, page_texts: List[str]) -> str:
        """检测绘本语言类型"""
        import re
        all_text = "".join(page_texts)
        chinese_chars = len(re.findall(r'[\u4e00-\u9fa5]', all_text))
        english_chars = len(re.findall(r'[a-zA-Z]', all_text))
        if chinese_chars > english_chars * 0.5:
            return "zh"
        else:
            return "en"


# ============================================================
# 命令行测试
# ============================================================

if __name__ == "__main__":
    import sys
    from config import image_analysis_url, image_api_key, image_model
    from config import simple_story_writer_url, simple_story_writer_api_key, simple_story_writer_model
    
    if len(sys.argv) < 2:
        print("用法: python pdf_book_reader.py <pdf_path> [max_pages]")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else None
    
    if not os.path.exists(pdf_path):
        print(f"❌ 文件不存在: {pdf_path}")
        sys.exit(1)
    
    reader = PDFBookReader(
        vl_base_url=image_analysis_url,
        vl_api_key=image_api_key,
        vl_model=image_model,
        llm_base_url=simple_story_writer_url,
        llm_api_key=simple_story_writer_api_key,
        llm_model=simple_story_writer_model,
        dpi=200
    )
    
    book_data = reader.read_book(
        pdf_path=pdf_path,
        max_pages=max_pages,
        verify_ocr=True
    )
    
    print("\n📊 解析结果概览：")
    print(f"  书名: {book_data['title']}")
    print(f"  页数: {book_data['total_pages']}")
    print(f"  语言: {book_data['language']}")
    print(f"  角色: {[c['name'] for c in book_data['characters']]}")
    print(f"  风格: {book_data['art_style_summary']}")
