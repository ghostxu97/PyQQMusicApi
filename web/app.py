"""WEB API Port (完整优化版)"""
import threading
from time import time
from typing import Any
import json  # 导入 json 模块
from pathlib import Path  # 导入 Path 模块

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel

import qqmusic_api
from examples.qrcode_login import qrcode_login_example
from qqmusic_api.login import QRLoginType
from web.parser import Parser


class ApiResponseModel(BaseModel):
    """API响应数据模型"""

    code: int
    message: str
    data: Any = None
    errors: list[str] | None = None
    timestamp: int


class ApiResponse(ORJSONResponse):
    """标准化API响应类"""

    def __init__(
        self,
        status_code: int = status.HTTP_200_OK,
        message: str = "Success",
        data: Any = None,
        errors: str | list[str] | None = None,
        **kwargs,
    ):
        # 错误信息标准化处理
        processed_errors = None
        if errors:
            processed_errors = [errors] if isinstance(errors, str) else errors

        # 构建响应内容
        content = ApiResponseModel(
            code=status_code, message=message, data=data, errors=processed_errors, timestamp=int(time())
        ).dict(exclude_unset=True, exclude_defaults=True)

        super().__init__(content=content, status_code=status_code, **kwargs)

    @classmethod
    def success(
        cls, data: Any = None, message: str = "Success", status_code: int = status.HTTP_200_OK
    ) -> "ApiResponse":
        """构建成功响应"""
        return cls(status_code=status_code, message=message, data=data)

    @classmethod
    def error(
        cls, errors: str | list[str], message: str = "Error", status_code: int = status.HTTP_400_BAD_REQUEST
    ) -> "ApiResponse":
        """构建错误响应"""
        return cls(status_code=status_code, message=message, errors=errors)


app = FastAPI(
    title="QQMusic API",
    description="QQMusic API Web Port",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    default_response_class=ApiResponse,  # 设置默认响应类
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# 在全局范围内存储凭证
global_credential = None  # 初始化凭证变量
credential_lock = threading.Lock()
is_logging_in = False  # 添加一个标志变量，用于指示是否正在执行登录操作

# 配置文件路径
CONFIG_FILE = "qqMusicCookies.json"
config_path = Path(CONFIG_FILE)


def load_credential_from_file() -> dict:
    """从文件中加载凭证"""
    try:
        if not config_path.exists():
            # 创建文件并写入空 JSON 对象
            with open(config_path, "w") as f:
                json.dump({"cookies": {}}, f, indent=4)
            print(f"配置文件 {CONFIG_FILE} 不存在，已自动创建")

        with open(config_path, "r") as f:
            config = json.load(f)
            return config.get("cookies", {})
    except Exception as e:
        print(f"加载配置文件时发生错误: {e}")
        return {}


def save_credential_to_file(credential: qqmusic_api.Credential):
    """将凭证保存到文件"""
    try:
        config = {"cookies": credential.as_dict()}
        with open(config_path, "w") as f:
            json.dump(config, f, indent=4)
        print("凭证已保存到文件")
    except Exception as e:
        print(f"保存凭证到文件时发生错误: {e}")


# 从配置文件加载 cookies
cookies: dict[str, Any] = load_credential_from_file()


@app.exception_handler(404)
async def _not_found_handler(request: Request, exc: HTTPException):
    return ApiResponse.error(errors="请求的资源不存在", message="Not Found", status_code=status.HTTP_404_NOT_FOUND)


@app.exception_handler(500)
async def _server_error_handler(request: Request, exc: HTTPException):
    return ApiResponse.error(
        errors=["服务器内部错误"], message="Internal Server Error", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
    )


@app.exception_handler(422)
async def _validation_error_handler(request: Request, exc: HTTPException):
    return ApiResponse.error(
        errors=["参数验证失败"], message="Validation Error", status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
    )


async def _api_web(
    request: Request,
    module: str,
    func: str,
):
    """统一API请求处理"""
    global global_credential  # 使用全局凭证变量
    global is_logging_in  # 使用全局变量来标识登录状态

    # 在函数最开始检查登录状态
    if is_logging_in:
        return ApiResponse.error(
            errors=["服务器正在登录，请稍后再试"], message="Service Unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    try:
        with credential_lock:  # 使用锁保护凭证的访问
            # 凭证处理
            if global_credential is None or not (global_credential.is_expired()):
                # 尝试从cookies中读取凭证
                try:
                    if not cookies:  # 处理cookies为空的情况
                        raise Exception("Cookies 为空，请在配置文件中配置")

                    credential = qqmusic_api.Credential.from_cookies_dict(cookies)
                    if not (credential.has_musicid() and credential.has_musickey() and credential.is_expired()):
                        raise Exception("Credential 无效")

                    global_credential = credential
                except Exception as e:
                    print(f"从cookie读取凭证失败: {e}，开始执行登录")
                    is_logging_in = True  # 设置登录标志
                    try:
                        global_credential = await qrcode_login_example(QRLoginType.QQ)  # 设置全局凭证
                        if global_credential:  # 登录成功后保存凭证
                            save_credential_to_file(global_credential)
                    finally:
                        is_logging_in = False  # 无论登录成功与否，都要重置登录标志

            if global_credential.can_refresh():
                # 刷新token过期时间
                await global_credential.refresh()

            # 使用全局变量
            credential = global_credential
            qqmusic_api.get_session().credential = credential  # 设置会话凭证
    except Exception:
        return ApiResponse.error(
            errors="无效的用户凭证", message="Unauthorized", status_code=status.HTTP_401_UNAUTHORIZED
        )

    # 参数解析
    params = dict(request.query_params)
    parser = Parser(module, func, params)

    # 执行解析
    try:
        result, errors = await parser.parse()
    except Exception:
        return ApiResponse.error(
            errors=["服务器处理请求时发生异常"],
            message="Internal Error",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # 错误处理
    if errors:
        #
        print(errors)
        if "QQ音乐API错误: 凭证已过期" in errors:
            with credential_lock:  # 在锁内重新登录
                # 重新登录 也需要考虑是否已经在登录中了，不要重复调用登录方法
                is_logging_in = True  # 设置登录标志
                try:
                    global_credential = await qrcode_login_example(QRLoginType.QQ)  # 设置全局凭证
                    if global_credential:  # 登录成功后保存凭证
                        save_credential_to_file(global_credential)
                finally:
                    is_logging_in = False  # 无论登录成功与否，都要重置登录标志
            # 重新登录 也需要考虑是否已经在登录中了，不要重复调用登录方法

        return ApiResponse.error(
            errors=errors, message="Request Validation Failed", status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    if not parser.valid:
        return ApiResponse.error(
            errors=["无效的请求参数"], message="Bad Request", status_code=status.HTTP_400_BAD_REQUEST
        )

    # 成功响应
    return ApiResponse.success(data=result, message="请求成功")


app.add_api_route(
    path="/{module}/{func}",
    endpoint=_api_web,
    methods=["GET"],
    responses={
        200: {"model": ApiResponseModel},
        400: {"model": ApiResponseModel},
        401: {"model": ApiResponseModel},
        422: {"model": ApiResponseModel},
        500: {"model": ApiResponseModel},
    },
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="debug",
    )
