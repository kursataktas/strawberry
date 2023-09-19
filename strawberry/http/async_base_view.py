import abc
import json
from typing import (
    Callable,
    Dict,
    Generic,
    Mapping,
    Optional,
    Union,
)

from strawberry import UNSET
from strawberry.exceptions import MissingQueryError
from strawberry.file_uploads.utils import replace_placeholders_with_files
from strawberry.http import GraphQLHTTPResponse, GraphQLRequestData, process_result
from strawberry.schema.base import BaseSchema
from strawberry.schema.exceptions import InvalidOperationTypeError
from strawberry.types import ExecutionResult
from strawberry.types.graphql import OperationType

from .base import BaseView
from .exceptions import HTTPException
from .types import FormData, HTTPMethod, QueryParams
from .typevars import Context, Request, Response, RootValue, SubResponse


class AsyncHTTPRequestAdapter(abc.ABC):
    @property
    @abc.abstractmethod
    def query_params(self) -> QueryParams:
        ...

    @property
    @abc.abstractmethod
    def method(self) -> HTTPMethod:
        ...

    @property
    @abc.abstractmethod
    def headers(self) -> Mapping[str, str]:
        ...

    @property
    @abc.abstractmethod
    def content_type(self) -> Optional[str]:
        ...

    @abc.abstractmethod
    async def get_body(self) -> Union[str, bytes]:
        ...

    @abc.abstractmethod
    async def get_form_data(self) -> FormData:
        ...


class AsyncBaseHTTPView(
    abc.ABC,
    BaseView[Request],
    Generic[Request, Response, SubResponse, Context, RootValue],
):
    schema: BaseSchema
    graphiql: bool
    request_adapter_class: Callable[[Request], AsyncHTTPRequestAdapter]

    @property
    @abc.abstractmethod
    def allow_queries_via_get(self) -> bool:
        ...

    @abc.abstractmethod
    async def get_sub_response(self, request: Request) -> SubResponse:
        ...

    @abc.abstractmethod
    async def get_context(self, request: Request, response: SubResponse) -> Context:
        ...

    @abc.abstractmethod
    async def get_root_value(self, request: Request) -> Optional[RootValue]:
        ...

    @abc.abstractmethod
    def render_graphiql(self, request: Request) -> Response:
        # TODO: this could be non abstract
        # maybe add a get template function?
        ...

    @abc.abstractmethod
    def create_response(
        self, response_data: GraphQLHTTPResponse, sub_response: SubResponse
    ) -> Response:
        ...

    @abc.abstractmethod
    def create_multipart_response(
        self, response_data: GraphQLHTTPResponse, sub_response: SubResponse
    ) -> Response:
        ...

    async def execute_operation(
        self, request: Request, context: Context, root_value: Optional[RootValue]
    ) -> ExecutionResult:
        request_adapter = self.request_adapter_class(request)

        try:
            request_data = await self.parse_http_body(request_adapter)
        except json.decoder.JSONDecodeError as e:
            raise HTTPException(400, "Unable to parse request body as JSON") from e
            # DO this only when doing files
        except KeyError as e:
            raise HTTPException(400, "File(s) missing in form data") from e

        allowed_operation_types = OperationType.from_http(request_adapter.method)

        if not self.allow_queries_via_get and request_adapter.method == "GET":
            allowed_operation_types = allowed_operation_types - {OperationType.QUERY}

        assert self.schema

        return await self.schema.subscribe(
            request_data.query,
            root_value=root_value,
            variable_values=request_data.variables,
            context_value=context,
            operation_name=request_data.operation_name,
        )

        return await self.schema.execute(
            request_data.query,
            root_value=root_value,
            variable_values=request_data.variables,
            context_value=context,
            operation_name=request_data.operation_name,
            allowed_operation_types=allowed_operation_types,
        )

    async def parse_multipart(self, request: AsyncHTTPRequestAdapter) -> Dict[str, str]:
        try:
            form_data = await request.get_form_data()
        except ValueError as e:
            raise HTTPException(400, "Unable to parse the multipart body") from e

        operations = form_data["form"].get("operations", "{}")
        files_map = form_data["form"].get("map", "{}")

        if isinstance(operations, (bytes, str)):
            operations = self.parse_json(operations)

        if isinstance(files_map, (bytes, str)):
            files_map = self.parse_json(files_map)

        try:
            return replace_placeholders_with_files(
                operations, files_map, form_data["files"]
            )
        except KeyError as e:
            raise HTTPException(400, "File(s) missing in form data") from e

    async def run(
        self,
        request: Request,
        context: Optional[Context] = UNSET,
        root_value: Optional[RootValue] = UNSET,
    ) -> Response:
        request_adapter = self.request_adapter_class(request)

        if not self.is_request_allowed(request_adapter):
            raise HTTPException(405, "GraphQL only supports GET and POST requests.")

        if self.should_render_graphiql(request_adapter):
            if self.graphiql:
                return self.render_graphiql(request)
            else:
                raise HTTPException(404, "Not Found")

        sub_response = await self.get_sub_response(request)
        context = (
            await self.get_context(request, response=sub_response)
            if context is UNSET
            else context
        )
        root_value = (
            await self.get_root_value(request) if root_value is UNSET else root_value
        )

        assert context

        try:
            result = await self.execute_operation(
                request=request, context=context, root_value=root_value
            )
        except InvalidOperationTypeError as e:
            raise HTTPException(
                400, e.as_http_error_reason(request_adapter.method)
            ) from e
        except MissingQueryError as e:
            raise HTTPException(400, "No GraphQL query found in the request") from e

        if hasattr(result, "__aiter__"):

            async def stream():
                async for value in result:
                    yield await self.process_result(request, value)

            return await self.create_multipart_response(stream(), sub_response)

        response_data = await self.process_result(request=request, result=result)

        return self.create_response(
            response_data=response_data, sub_response=sub_response
        )

    async def parse_multipart_subscriptions(
        self, request: AsyncHTTPRequestAdapter
    ) -> Dict[str, str]:
        return self.parse_json(await request.get_body())

    async def parse_http_body(
        self, request: AsyncHTTPRequestAdapter
    ) -> GraphQLRequestData:
        content_type = request.content_type or ""

        if "application/json" in content_type:
            data = self.parse_json(await request.get_body())
        elif content_type.startswith("multipart/form-data"):
            data = await self.parse_multipart(request)
        elif content_type.startswith("multipart/mixed"):
            # TODO: do a check that checks if this is a multipart subscription
            data = await self.parse_multipart_subscriptions(request)
        elif request.method == "GET":
            data = self.parse_query_params(request.query_params)
        else:
            raise HTTPException(400, "Unsupported content type")

        return GraphQLRequestData(
            query=data.get("query"),
            variables=data.get("variables"),  # type: ignore
            operation_name=data.get("operationName"),
        )

    async def process_result(
        self, request: Request, result: ExecutionResult
    ) -> GraphQLHTTPResponse:
        return process_result(result)
