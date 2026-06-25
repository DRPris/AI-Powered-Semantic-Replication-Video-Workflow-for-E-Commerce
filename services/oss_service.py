import logging
import os
from pathlib import Path
from typing import Optional
import asyncio

logger = logging.getLogger(__name__)


class OSSService:
    """阿里云 OSS 文件存储服务"""
    
    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        bucket_name: str,
        endpoint: str,
        cdn_domain: str = "",
    ):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.bucket_name = bucket_name
        self.endpoint = endpoint
        self.cdn_domain = cdn_domain
        self._bucket = None
    
    def _get_bucket(self):
        """延迟初始化 OSS bucket 连接"""
        if self._bucket is None:
            try:
                import oss2
                # 清除代理环境变量，避免 SOCKS 代理导致连接问题
                import os
                for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
                    os.environ.pop(key, None)
                
                auth = oss2.Auth(self.access_key_id, self.access_key_secret)
                self._bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
                logger.info(f"OSS bucket 初始化成功: {self.bucket_name}")
            except ImportError:
                logger.error("oss2 库未安装，请执行: pip install oss2")
                raise
        return self._bucket
    
    async def upload_file(
        self,
        local_path: str,
        oss_key: Optional[str] = None,
        content_type: Optional[str] = None,
        expires: int = 7200,
    ) -> str:
        """
        上传文件到 OSS。
        
        Args:
            local_path: 本地文件路径
            oss_key: OSS 对象键（可选，默认自动生成）
            content_type: 文件 MIME 类型
            expires: 签名URL有效期（秒），默认7200秒（2小时）
        
        Returns:
            文件的签名访问 URL
        """
        if oss_key is None:
            import uuid
            ext = Path(local_path).suffix
            oss_key = f"video-replication/{uuid.uuid4().hex}{ext}"
        
        bucket = self._get_bucket()
        
        # 上传文件（同步操作，用 to_thread 包装）
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        
        await asyncio.to_thread(
            bucket.put_object_from_file,
            oss_key,
            local_path,
            headers=headers if headers else None,
        )
        
        logger.info(f"上传到 OSS: {oss_key}")
        
        # 返回签名URL
        return self.get_signed_url(oss_key, expires)
    
    async def upload_bytes(
        self,
        data: bytes,
        oss_key: str,
        content_type: Optional[str] = None,
        expires: int = 7200,
    ) -> str:
        """上传字节数据到 OSS，返回签名URL"""
        bucket = self._get_bucket()
        
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        
        await asyncio.to_thread(
            bucket.put_object,
            oss_key,
            data,
            headers=headers if headers else None,
        )
        
        logger.info(f"上传到 OSS: {oss_key}")
        return self.get_signed_url(oss_key, expires)
    
    def get_url(self, oss_key: str) -> str:
        """获取文件的可访问 URL（非签名，需Bucket为公共读）"""
        if self.cdn_domain:
            return f"https://{self.cdn_domain}/{oss_key}"
        else:
            return f"https://{self.bucket_name}.{self.endpoint}/{oss_key}"
    
    def get_signed_url(self, oss_key: str, expires: int = 7200) -> str:
        """
        获取文件的签名访问 URL（用于私有Bucket）
        
        Args:
            oss_key: OSS 对象键
            expires: 签名有效期（秒），默认7200秒（2小时）
        
        Returns:
            签名URL
        """
        bucket = self._get_bucket()
        signed_url = bucket.sign_url('GET', oss_key, expires)
        logger.debug(f"生成签名URL: {oss_key}, 有效期: {expires}秒")
        return signed_url
    
    async def delete_file(self, oss_key: str) -> None:
        """删除 OSS 上的文件"""
        bucket = self._get_bucket()
        await asyncio.to_thread(bucket.delete_object, oss_key)
        logger.info(f"删除 OSS 文件: {oss_key}")
    
    async def file_exists(self, oss_key: str) -> bool:
        """检查文件是否存在"""
        bucket = self._get_bucket()
        return await asyncio.to_thread(bucket.object_exists, oss_key)
