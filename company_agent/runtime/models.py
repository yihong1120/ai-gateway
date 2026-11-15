"""Official LangChain model integration for the configured LiteLLM endpoint."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from urllib.parse import urlsplit

import httpx
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from openai import APIConnectionError
from openai import APITimeoutError
from openai import RateLimitError

from examples.identity.providers.subjects import linked_subject

from examples.identity.adapters import webui as webui_identity
from company_agent.runtime.gemini import provider_tools
from company_agent.runtime.gemini import signed_history


def model_failure_message(failure):
    """Safe actionable descriptions; never expose provider bodies or retry I/O."""
    if isinstance(failure, RateLimitError):
        return '模型服務目前受到速率或額度限制（429），本次未完成。既有成果已保留；請稍後至工作台明確選擇「恢復」接續，未自動重送。'
    if isinstance(failure, (APITimeoutError, TimeoutError)):
        return '工作或模型回應逾時，本次未完成。既有成果已保留；請至工作台查看狀態並明確恢復，未自動重送。'
    if isinstance(failure, APIConnectionError):
        return '模型服務連線中斷，本次未完成。既有成果已保留；請確認連線後至工作台明確恢復，未自動重送。'
    return None


async def close_model(model):
    """Release the HTTP clients owned by GatewayModel; no application retries."""
    if isinstance(model, UserBilledModel):
        model = model.client
    await model.http_async_client.aclose()
    model.http_client.close()


def request_options(client):
    timeout = getattr(client, 'request_timeout', None)
    options = {'timeout': timeout} if timeout is not None else {}
    options['extra_body'] = {
        **(getattr(client, 'extra_body', None) or {}),
        'metadata': {'spend_logs_metadata': {'ai_usage_source': 'company_agent'}},
    }
    return options


@dataclass(frozen=True)
class UserBilledModel:
    """Server-created binding; never accept this identity from request text/tools."""

    owner: str
    subject: str
    client: ChatOpenAI = field(repr=False)
    routes: dict = field(default_factory=dict, repr=False)
    profiles: dict = field(default_factory=dict, repr=False)

    def for_profile(self, name, *, images=False):
        profile = self.profiles.get(name)
        if profile is None:
            raise ValueError('所選模型不在本工作可用清單內，未呼叫模型。')
        if images and not profile.supports_images:
            raise ValueError('所選模型未配置圖片能力，未傳送圖片。')
        return self.with_route(profile)

    def for_task(self, name):
        route = self.routes.get(name)
        if route is not None:
            return self.with_route(route)
        if name == 'summary':
            return replace(self, client=self.client.model_copy(update={'max_tokens': 2048}), routes={})
        return self

    def with_route(self, route):
        # Share transports and preserve the identity subclass; never mutate clients.
        client = self.client.model_copy(
            update={
                'model_name': route.model,
                'reasoning_effort': route.reasoning_effort,
                'max_tokens': route.max_tokens,
                'request_timeout': route.request_timeout_seconds or getattr(self.client, 'request_timeout', None),
            },
        )
        return replace(self, client=client, routes={})

    def checked(self, runnable):
        """Web bindings add per-invocation identity verification at this boundary."""
        from examples.shared.runtime.concurrency import MODELS

        async def limited(messages, config):
            async with MODELS:
                return await runnable.ainvoke(messages, config=config)

        return RunnableLambda(limited)

    async def analyze(self, messages, *, schema=None, max_output_tokens=None):
        response_format = {'type': 'json_object'} if schema is None else {
            'type': 'json_schema', 'json_schema': {'name': 'analysis', 'schema': schema},
        }
        options = {'response_format': response_format}
        if max_output_tokens is not None:
            options['max_tokens'] = max_output_tokens
        return await self.checked(self.client.bind(user=self.subject, **request_options(self.client), **options)).ainvoke(messages)

    async def summarize(self, prompt):
        return await self.checked(self.client.bind(user=self.subject, **request_options(self.client))).ainvoke(prompt)

    def bind_tools(self, tools, **kwargs):
        if any(k in kwargs for k in ('user', 'extra_body', 'extra_headers', 'model_kwargs')):
            raise ValueError('不允許覆寫已驗證的使用者計費設定。')
        name = self.client.model_name
        bound = self.client.bind_tools(
            provider_tools(tools, name), user=self.subject, **request_options(self.client), **kwargs,
        )
        if name.lower().split('/')[-1].startswith('gemini-'):
            bound = RunnableLambda(signed_history) | bound
        return self.checked(bound)


def select_model(model, name):
    return model.for_task(name) if isinstance(model, UserBilledModel) else model


def routed_model(model, settings):
    if not settings.role_models and not settings.purpose_models and not settings.model_profiles and settings.model_timeout_seconds == 60:
        return model
    if not isinstance(model, UserBilledModel):
        raise ValueError('分流設定需要已驗證的個人計費模型。')
    client = model.client
    if getattr(client, 'request_timeout', 60) != settings.model_timeout_seconds:
        client = client.model_copy(update={'request_timeout': settings.model_timeout_seconds})
    return replace(
        model, client=client, routes={
            k: v.model_copy(deep=True)
            for k, v in {**settings.role_models, **settings.purpose_models}.items()
        }, profiles={k: v.model_copy(deep=True) for k, v in settings.model_profiles.items()},
    )


def model_policy(model):
    if not isinstance(model, UserBilledModel):
        return None
    policy = {
        'default': model.client.model_name,
        'reasoning_effort': model.client.reasoning_effort,
        'max_tokens': model.client.max_tokens,
        'request_timeout_seconds': getattr(model.client, 'request_timeout', 60),
        'routes': {k: v.model_dump() for k, v in model.routes.items()},
    }
    if model.profiles:
        policy['profiles'] = {k: v.model_dump() for k, v in model.profiles.items()}
    return policy


def select_profile(model, name, *, images=False):
    if not isinstance(model, UserBilledModel):
        raise ValueError('動態選模需要已驗證的個人計費身分。')
    return model.for_profile(name, images=images)


def analysis_model(model, profile, operation):
    if profile:
        return select_profile(model, profile, images=operation == 'vision')
    # Explicit profiles take precedence; without a catalog, use purpose routes.
    if isinstance(model, UserBilledModel) and model.profiles:
        return model
    return select_model(model, operation)


def gateway_for_verified_user(user, base_url, api_key, model, transport=None):
    """Call only with the server's authenticated UserModel projection.

    This function validates the projection, not a browser JWT signature.
    The HTTP integration must authenticate first, just like Open WebUI's filter.
    """
    if not isinstance(user, dict) or user.get('role') not in ('user', 'admin'):
        raise ValueError('須先驗證啟用中的使用者身分，未呼叫模型。')
    owner = user.get('id')
    subject = linked_subject(user.get('oauth'))
    if any(
        not isinstance(v, str) or not v.strip() or v != v.strip() or len(v) > 255
        for v in (owner, subject)
    ):
        raise ValueError('缺少有效的使用者 ID／Keycloak subject，未繞過個人額度。')
    return UserBilledModel(owner, subject, GatewayModel(base_url, api_key, model, transport))


def GatewayModel(base_url, api_key, model, transport=None):
    url = urlsplit(base_url)
    if (
        url.scheme not in ('http', 'https')
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or (url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1'))
    ):
        raise ValueError('模型端點必須為 HTTPS 或本機 loopback HTTP。')
    if not api_key or not model:
        raise ValueError('必須明確設定模型與專用 API key。')
    return ChatOpenAI(
        model=model,
        base_url=base_url.rstrip('/'),
        api_key=api_key,
        max_retries=0,
        timeout=60,
        max_tokens=4096,
        use_responses_api=False,
        extra_body={'num_retries': 0, 'fallbacks': [], 'max_fallbacks': 0},
        http_async_client=httpx.AsyncClient(
            timeout=60, follow_redirects=False, trust_env=False, transport=transport,
        ),
        http_client=httpx.Client(timeout=60, follow_redirects=False, trust_env=False),
    )


async def billed_analysis(ctx, messages, *, purpose='vision', model_profile=None, schema=None, max_output_tokens=None):
    if not isinstance(ctx.model, UserBilledModel) or ctx.model.owner != ctx.owner:
        raise ValueError('分析需要本人一般 LiteLLM 計費模型；不使用公文免扣額度金鑰。')
    model = analysis_model(ctx.model, model_profile, purpose)
    ctx.store.reserve_model_attempt(ctx.owner, ctx.job, ctx.settings.max_steps)
    key = 'analysis:' + uuid.uuid4().hex
    ctx.store.begin_tool(ctx.owner, ctx.job, key, 'billed_analysis', {})
    options = {}
    if schema is not None:
        options['schema'] = schema
    if max_output_tokens is not None:
        options['max_output_tokens'] = max_output_tokens
    with tracing_context(enabled=False):
        message = await model.analyze(messages, **options)
    ctx.store.finish_tool(
        ctx.owner, ctx.job, key, {
            'usage': message.usage_metadata or {},
            'model': model.client.model_name,
            'purpose': purpose,
            'model_profile': model_profile,
        },
    )
    if (
        message.tool_calls
        or message.invalid_tool_calls
        or message.response_metadata.get('finish_reason') not in (None, 'stop')
        or not isinstance(message.content, str)
        or not message.content.strip()
        or len(message.content) > 24000
    ):
        raise ValueError('分析回覆空白、截斷或格式不符，未自動重試。')
    return message.content


class WebBilledModel(UserBilledModel):
    def checked(self, runnable):
        return super().checked(RunnableLambda(self.verified_messages) | runnable)

    async def verified_messages(self, messages):
        user = await webui_identity.verified_user(self.owner)
        if (user.get('oauth') or {}).get('oidc', {}).get('sub') != self.subject:
            raise ValueError('登入計費身分已變更，已停止後續模型請求。')
        return messages
