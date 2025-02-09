from abc import abstractmethod
from collections import defaultdict, deque, namedtuple
from typing import Deque, Dict, Iterable, Optional, Sequence, Set, Tuple, Union, cast

from pydantic.error_wrappers import ValidationError

import dipdup.models as models
from dipdup.config import (
    BigMapHandlerConfig,
    BigMapIndexConfig,
    ContractConfig,
    HeadHandlerConfig,
    HeadIndexConfig,
    OperationHandlerConfig,
    OperationHandlerOriginationPatternConfig,
    OperationHandlerPatternConfigT,
    OperationHandlerTransactionPatternConfig,
    OperationIndexConfig,
    OperationType,
    ResolvedIndexConfigT,
)
from dipdup.context import DipDupContext
from dipdup.datasources.tzkt.datasource import BigMapFetcher, OperationFetcher, TzktDatasource
from dipdup.exceptions import ConfigInitializationException, InvalidDataError, ReindexingReason
from dipdup.models import BigMapData, BigMapDiff, BlockData, HeadBlockData, IndexStatus, OperationData, Origination, Transaction
from dipdup.utils import FormattedLogger
from dipdup.utils.database import in_global_transaction

# NOTE: Operations of a single contract call
OperationSubgroup = namedtuple('OperationSubgroup', ('hash', 'counter'))

# NOTE: Message queue of OperationIndex
SingleLevelRollback = namedtuple('SingleLevelRollback', ('level'))
Operations = Tuple[OperationData, ...]
OperationQueueItemT = Union[Operations, SingleLevelRollback]
OperationHandlerArgumentT = Optional[Union[Transaction, Origination, OperationData]]
MatchedOperationsT = Tuple[OperationSubgroup, OperationHandlerConfig, Deque[OperationHandlerArgumentT]]
MatchedBigMapsT = Tuple[BigMapHandlerConfig, BigMapDiff]

# NOTE: For initializing the index state on startup
block_cache: Dict[int, BlockData] = {}


class Index:
    """Base class for index implementations

    Provides common interface for managing index state and switching between sync and realtime modes.
    """

    _queue: Deque

    def __init__(self, ctx: DipDupContext, config: ResolvedIndexConfigT, datasource: TzktDatasource) -> None:
        self._ctx = ctx
        self._config = config
        self._datasource = datasource

        self._logger = FormattedLogger('dipdup.index', fmt=f'{config.name}: ' + '{}')
        self._state: Optional[models.Index] = None

    @property
    def datasource(self) -> TzktDatasource:
        return self._datasource

    @property
    def state(self) -> models.Index:
        if self._state is None:
            raise RuntimeError('Index state is not initialized')
        return self._state

    async def initialize_state(self) -> None:
        if self._state:
            raise RuntimeError('Index state is already initialized')

        if isinstance(self._config, (OperationIndexConfig, BigMapIndexConfig)) and self._config.first_level:
            level = self._config.first_level
        else:
            level = 0

        self._state, created = await models.Index.get_or_create(
            name=self._config.name,
            type=self._config.kind,
            defaults=dict(
                level=level,
                config_hash=self._config.hash(),
                template=self._config.parent.name if self._config.parent else None,
                template_values=self._config.template_values,
            ),
        )

        if created or not self._state.level:
            return

        head = await models.Head.filter(name=self.datasource.name).order_by('-level').first()
        if not head:
            return

        if head.level not in block_cache:
            block_cache[head.level] = await self.datasource.get_block(head.level)
        if head.hash != block_cache[head.level].hash:
            await self._ctx.reindex(ReindexingReason.BLOCK_HASH_MISMATCH)

    async def process(self) -> None:
        # NOTE: `--oneshot` flag implied
        if isinstance(self._config, (OperationIndexConfig, BigMapIndexConfig)) and self._config.last_level:
            last_level = self._config.last_level
            await self._synchronize(last_level, cache=True)
            await self.state.update_status(IndexStatus.ONESHOT, last_level)

        if self._datasource.sync_level is None:
            raise RuntimeError('Call `set_sync_level` before starting IndexDispatcher')

        elif self.state.level < self._datasource.sync_level:
            self._logger.info(
                'Index is behind datasource, sync to datasource level: %s -> %s', self.state.level, self._datasource.sync_level
            )
            self._queue.clear()
            last_level = self._datasource.sync_level
            await self._synchronize(last_level)

        else:
            await self._process_queue()

    @abstractmethod
    async def _synchronize(self, last_level: int, cache: bool = False) -> None:
        ...

    @abstractmethod
    async def _process_queue(self) -> None:
        ...

    async def _enter_sync_state(self, last_level: int) -> Optional[int]:
        if self.state.status == IndexStatus.ONESHOT:
            return None

        first_level = self.state.level

        if first_level == last_level:
            return None
        if first_level > last_level:
            raise RuntimeError(f'Attempt to synchronize index from level {first_level} to level {last_level}')

        self._logger.info('Synchronizing index to level %s', last_level)
        await self.state.update_status(status=IndexStatus.SYNCING, level=first_level)
        return first_level

    async def _exit_sync_state(self, last_level: int) -> None:
        self._logger.info('Index is synchronized to level %s', last_level)
        await self.state.update_status(status=IndexStatus.REALTIME, level=last_level)

    def _extract_level(self, message: Union[Tuple[OperationData, ...], Tuple[BigMapData, ...]]) -> int:
        batch_levels = tuple(set(item.level for item in message))
        if len(batch_levels) != 1:
            raise RuntimeError(f'Items in operation/big_map batch have different levels: {batch_levels}')
        return tuple(batch_levels)[0]


class OperationIndex(Index):
    _config: OperationIndexConfig

    def __init__(self, ctx: DipDupContext, config: OperationIndexConfig, datasource: TzktDatasource) -> None:
        super().__init__(ctx, config, datasource)
        self._queue: Deque[OperationQueueItemT] = deque()
        self._contract_hashes: Dict[str, Tuple[int, int]] = {}
        self._rollback_level: Optional[int] = None
        self._head_hashes: Set[str] = set()
        self._migration_originations: Optional[Dict[str, OperationData]] = None

    def push_operations(self, operations: Tuple[OperationData, ...]) -> None:
        self._queue.append(operations)

    def push_rollback(self, level: int) -> None:
        self._queue.append(SingleLevelRollback(level))

    async def _single_level_rollback(self, level: int) -> None:
        """Ensure next arrived block has all operations of the previous block. But it could also contain additional operations.

        Called by IndexDispatcher when index datasource receive a single level rollback.
        """
        if self._rollback_level:
            raise RuntimeError('Index is already in rollback state')

        state_level = cast(int, self.state.level)
        if state_level < level:
            self._logger.info('Index level is lower than rollback level, ignoring: %s < %s', state_level, level)
        elif state_level == level:
            self._logger.info('Single level rollback, next block will be processed partially')
            self._rollback_level = level
        else:
            raise RuntimeError(f'Index level is higher than rollback level: {state_level} > {level}')

    async def _process_queue(self) -> None:
        """Process WebSocket queue"""
        while self._queue:
            message = self._queue.popleft()
            if isinstance(message, SingleLevelRollback):
                self._logger.info('Processing rollback realtime message, %s left in queue', len(self._queue))
                await self._single_level_rollback(message.level)
            else:
                self._logger.info('Processing operations realtime message, %s left in queue', len(self._queue))
                await self._process_level_operations(message)

    async def _synchronize(self, last_level: int, cache: bool = False) -> None:
        """Fetch operations via Fetcher and pass to message callback"""
        first_level = await self._enter_sync_state(last_level)
        if first_level is None:
            return

        self._logger.info('Fetching operations from level %s to %s', first_level, last_level)
        transaction_addresses = await self._get_transaction_addresses()
        origination_addresses = await self._get_origination_addresses()

        migration_originations: Tuple[OperationData, ...] = ()
        if self._config.types and OperationType.migration in self._config.types:
            migration_originations = tuple(await self._datasource.get_migration_originations(first_level))
            for op in migration_originations:
                code_hash, type_hash = await self._get_contract_hashes(cast(str, op.originated_contract_address))
                op.originated_contract_code_hash, op.originated_contract_type_hash = code_hash, type_hash

        fetcher = OperationFetcher(
            datasource=self._datasource,
            first_level=first_level,
            last_level=last_level,
            transaction_addresses=transaction_addresses,
            origination_addresses=origination_addresses,
            cache=cache,
            migration_originations=migration_originations,
        )

        async for _, operations in fetcher.fetch_operations_by_level():
            await self._process_level_operations(operations)

        await self._exit_sync_state(last_level)

    async def _process_level_operations(self, operations: Tuple[OperationData, ...]) -> None:
        if not operations:
            return
        level = self._extract_level(operations)

        if self._rollback_level:
            levels = {
                'operations': level,
                'rollback': self._rollback_level,
                'index': self.state.level,
            }
            if len(set(levels.values())) != 1:
                levels_repr = ', '.join(f'{k}={v}' for k, v in levels.items())
                raise RuntimeError(f'Index is in a rollback state, but received operation batch with different levels: {levels_repr}')

            self._logger.info('Rolling back to previous level, verifying processed operations')
            expected_hashes = set(self._head_hashes)
            received_hashes = set(op.hash for op in operations)
            new_hashes = received_hashes - expected_hashes
            missing_hashes = expected_hashes - received_hashes

            self._logger.info('Comparing hashes: %s new, %s missing', len(new_hashes), len(missing_hashes))
            if missing_hashes:
                self._logger.info('Some operations are backtracked: %s', ', '.join(missing_hashes))
                await self._ctx.reindex(ReindexingReason.ROLLBACK)

            self._rollback_level = None
            self._head_hashes = set()
            operations = tuple(op for op in operations if op.hash in new_hashes)

        # NOTE: le operator because it could be a single level rollback
        elif level < self.state.level:
            raise RuntimeError(f'Level of operation batch must be higher than index state level: {level} < {self.state.level}')

        self._logger.info('Processing %s operations of level %s', len(operations), level)
        matched_subgroups = await self._match_operations(operations)

        # NOTE: We still need to bump index level but don't care if it will be done in existing transaction
        if not matched_subgroups:
            await self.state.update_status(level=level)
            return

        async with in_global_transaction():
            for operation_subgroup, handler_config, args in matched_subgroups:
                await self._call_matched_handler(handler_config, operation_subgroup, args)
            await self.state.update_status(level=level)

    async def _match_operation(self, pattern_config: OperationHandlerPatternConfigT, operation: OperationData) -> bool:
        """Match single operation with pattern"""
        # NOTE: Reversed conditions are intentional
        if isinstance(pattern_config, OperationHandlerTransactionPatternConfig):
            if pattern_config.entrypoint != operation.entrypoint:
                return False
            if pattern_config.destination:
                if pattern_config.destination_contract_config.address != operation.target_address:
                    return False
            if pattern_config.source:
                if pattern_config.source_contract_config.address != operation.sender_address:
                    return False
            return True

        elif isinstance(pattern_config, OperationHandlerOriginationPatternConfig):
            if pattern_config.source:
                if pattern_config.source_contract_config.address != operation.sender_address:
                    return False
            if pattern_config.originated_contract:
                if pattern_config.originated_contract_config.address != operation.originated_contract_address:
                    return False
            if pattern_config.similar_to:
                code_hash, type_hash = await self._get_contract_hashes(pattern_config.similar_to_contract_config.address)
                if pattern_config.strict:
                    if code_hash != operation.originated_contract_code_hash:
                        return False
                else:
                    if type_hash != operation.originated_contract_type_hash:
                        return False
            return True
        else:
            raise NotImplementedError

    async def _match_operations(self, operations: Iterable[OperationData]) -> Deque[MatchedOperationsT]:
        """Try to match operations in cache with all patterns from indexes. Must be wrapped in transaction."""
        self._head_hashes.clear()
        matched_subgroups: Deque[MatchedOperationsT] = deque()
        operation_subgroups: Dict[OperationSubgroup, Deque[OperationData]] = defaultdict(deque)
        for operation in operations:
            key = OperationSubgroup(operation.hash, operation.counter)
            operation_subgroups[key].append(operation)
            self._head_hashes.add(operation.hash)

        for operation_subgroup, operations in operation_subgroups.items():
            self._logger.debug('Matching %s', key)

            for handler_config in self._config.handlers:
                operation_idx = 0
                pattern_idx = 0
                matched_operations: Deque[Optional[OperationData]] = deque()

                # TODO: Ensure complex cases work, e.g. when optional argument is followed by required one
                # TODO: Add None to matched_operations where applicable (pattern is optional and operation not found)
                while operation_idx < len(operations):
                    operation, pattern_config = operations[operation_idx], handler_config.pattern[pattern_idx]
                    operation_matched = await self._match_operation(pattern_config, operation)

                    if operation.type == 'origination' and isinstance(pattern_config, OperationHandlerOriginationPatternConfig):

                        if operation_matched is True and pattern_config.origination_processed(
                            cast(str, operation.originated_contract_address)
                        ):
                            operation_matched = False

                    if operation_matched:
                        matched_operations.append(operation)
                        pattern_idx += 1
                        operation_idx += 1
                    elif pattern_config.optional:
                        matched_operations.append(None)
                        pattern_idx += 1
                    else:
                        operation_idx += 1

                    if pattern_idx == len(handler_config.pattern):
                        self._logger.info('%s: `%s` handler matched!', operation_subgroup.hash, handler_config.callback)

                        args = await self._prepare_handler_args(handler_config, matched_operations)
                        matched_subgroups.append((operation_subgroup, handler_config, args))

                        matched_operations.clear()
                        pattern_idx = 0

                if len(matched_operations) >= sum(map(lambda x: 0 if x.optional else 1, handler_config.pattern)):
                    self._logger.info('%s: `%s` handler matched!', operation_subgroup.hash, handler_config.callback)

                    args = await self._prepare_handler_args(handler_config, matched_operations)
                    matched_subgroups.append((operation_subgroup, handler_config, args))

        return matched_subgroups

    async def _prepare_handler_args(
        self,
        handler_config: OperationHandlerConfig,
        matched_operations: Deque[Optional[OperationData]],
    ) -> Deque[OperationHandlerArgumentT]:
        """Prepare handler arguments, parse parameter and storage."""
        args: Deque[OperationHandlerArgumentT] = deque()
        for pattern_config, operation in zip(handler_config.pattern, matched_operations):
            if operation is None:
                args.append(None)

            elif isinstance(pattern_config, OperationHandlerTransactionPatternConfig):
                if not pattern_config.entrypoint:
                    args.append(operation)
                    continue

                parameter_type = pattern_config.parameter_type_cls
                try:
                    parameter = parameter_type.parse_obj(operation.parameter_json) if parameter_type else None
                except ValidationError as e:
                    raise InvalidDataError(parameter_type, operation.parameter_json, operation) from e

                storage_type = pattern_config.storage_type_cls
                storage = operation.get_merged_storage(storage_type)

                transaction_context = Transaction(
                    data=operation,
                    parameter=parameter,
                    storage=storage,
                )
                args.append(transaction_context)

            elif isinstance(pattern_config, OperationHandlerOriginationPatternConfig):
                storage_type = pattern_config.storage_type_cls
                storage = operation.get_merged_storage(storage_type)

                origination_context = Origination(
                    data=operation,
                    storage=storage,
                )
                args.append(origination_context)

            else:
                raise NotImplementedError

        return args

    async def _call_matched_handler(
        self, handler_config: OperationHandlerConfig, operation_subgroup: OperationSubgroup, args: Sequence[OperationHandlerArgumentT]
    ) -> None:
        if not handler_config.parent:
            raise ConfigInitializationException

        await self._ctx.fire_handler(
            handler_config.callback,
            handler_config.parent.name,
            self.datasource,
            operation_subgroup.hash + ': {}',
            *args,
        )

    async def _get_transaction_addresses(self) -> Set[str]:
        """Get addresses to fetch transactions from during initial synchronization"""
        if self._config.types and OperationType.transaction not in self._config.types:
            return set()
        return set(cast(ContractConfig, c).address for c in self._config.contracts or [])

    async def _get_origination_addresses(self) -> Set[str]:
        """Get addresses to fetch origination from during initial synchronization"""
        addresses = set()
        for handler_config in self._config.handlers:
            for pattern_config in handler_config.pattern:
                if isinstance(pattern_config, OperationHandlerOriginationPatternConfig):
                    if pattern_config.originated_contract:
                        addresses.add(pattern_config.originated_contract_config.address)
                    if pattern_config.source:
                        for address in await self._datasource.get_originated_contracts(pattern_config.source_contract_config.address):
                            addresses.add(address)
                    if pattern_config.similar_to:
                        for address in await self._datasource.get_similar_contracts(
                            address=pattern_config.similar_to_contract_config.address,
                            strict=pattern_config.strict,
                        ):
                            addresses.add(address)
        return addresses

    async def _get_contract_hashes(self, address: str) -> Tuple[int, int]:
        if address not in self._contract_hashes:
            summary = await self._datasource.get_contract_summary(address)
            self._contract_hashes[address] = (summary['codeHash'], summary['typeHash'])
        return self._contract_hashes[address]


class BigMapIndex(Index):
    _config: BigMapIndexConfig

    def __init__(self, ctx: DipDupContext, config: BigMapIndexConfig, datasource: TzktDatasource) -> None:
        super().__init__(ctx, config, datasource)
        self._queue: Deque[Tuple[BigMapData, ...]] = deque()

    def push_big_maps(self, big_maps: Tuple[BigMapData, ...]) -> None:
        self._queue.append(big_maps)

    async def _process_queue(self) -> None:
        """Process WebSocket queue"""
        if self._queue:
            self._logger.info('Processing websocket queue')
        while self._queue:
            big_maps = self._queue.popleft()
            await self._process_level_big_maps(big_maps)

    async def _synchronize(self, last_level: int, cache: bool = False) -> None:
        """Fetch operations via Fetcher and pass to message callback"""
        first_level = await self._enter_sync_state(last_level)
        if first_level is None:
            return

        self._logger.info('Fetching big map diffs from level %s to %s', first_level, last_level)

        big_map_addresses = await self._get_big_map_addresses()
        big_map_paths = await self._get_big_map_paths()

        fetcher = BigMapFetcher(
            datasource=self._datasource,
            first_level=first_level,
            last_level=last_level,
            big_map_addresses=big_map_addresses,
            big_map_paths=big_map_paths,
            cache=cache,
        )

        async for _, big_maps in fetcher.fetch_big_maps_by_level():
            await self._process_level_big_maps(big_maps)

        await self._exit_sync_state(last_level)

    async def _process_level_big_maps(self, big_maps: Tuple[BigMapData, ...]):
        if not big_maps:
            return
        level = self._extract_level(big_maps)

        # NOTE: le operator because single level rollbacks are not supported
        if level <= self.state.level:
            raise RuntimeError(f'Level of big map batch must be higher than index state level: {level} <= {self.state.level}')

        self._logger.info('Processing %s big map diffs of level %s', len(big_maps), level)
        matched_big_maps = await self._match_big_maps(big_maps)

        # NOTE: We still need to bump index level but don't care if it will be done in existing transaction
        if not matched_big_maps:
            await self.state.update_status(level=level)
            return

        async with in_global_transaction():
            for handler_config, big_map_diff in matched_big_maps:
                await self._call_matched_handler(handler_config, big_map_diff)
            await self.state.update_status(level=level)

    async def _match_big_map(self, handler_config: BigMapHandlerConfig, big_map: BigMapData) -> bool:
        """Match single big map diff with pattern"""
        if handler_config.path != big_map.path:
            return False
        if handler_config.contract_config.address != big_map.contract_address:
            return False
        return True

    async def _prepare_handler_args(
        self,
        handler_config: BigMapHandlerConfig,
        matched_big_map: BigMapData,
    ) -> BigMapDiff:
        """Prepare handler arguments, parse key and value. Schedule callback in executor."""
        self._logger.info('%s: `%s` handler matched!', matched_big_map.operation_id, handler_config.callback)
        if not handler_config.parent:
            raise ConfigInitializationException

        if matched_big_map.action.has_key:
            key_type = handler_config.key_type_cls
            try:
                key = key_type.parse_obj(matched_big_map.key)
            except ValidationError as e:
                raise InvalidDataError(key_type, matched_big_map.key, matched_big_map) from e
        else:
            key = None

        if matched_big_map.action.has_value:
            value_type = handler_config.value_type_cls
            try:
                value = value_type.parse_obj(matched_big_map.value)
            except ValidationError as e:
                raise InvalidDataError(value_type, matched_big_map.key, matched_big_map) from e
        else:
            value = None

        return BigMapDiff(
            data=matched_big_map,
            action=matched_big_map.action,
            key=key,
            value=value,
        )

    async def _match_big_maps(self, big_maps: Iterable[BigMapData]) -> Deque[MatchedBigMapsT]:
        """Try to match big map diffs in cache with all patterns from indexes."""
        matched_big_maps: Deque[MatchedBigMapsT] = deque()

        for big_map in big_maps:
            for handler_config in self._config.handlers:
                big_map_matched = await self._match_big_map(handler_config, big_map)
                if big_map_matched:
                    arg = await self._prepare_handler_args(handler_config, big_map)
                    matched_big_maps.append((handler_config, arg))

        return matched_big_maps

    async def _call_matched_handler(self, handler_config: BigMapHandlerConfig, big_map_diff: BigMapDiff) -> None:
        if not handler_config.parent:
            raise ConfigInitializationException

        await self._ctx.fire_handler(
            handler_config.callback,
            handler_config.parent.name,
            self.datasource,
            # FIXME: missing `operation_id` field in API to identify operation
            None,
            big_map_diff,
        )

    async def _get_big_map_addresses(self) -> Set[str]:
        """Get addresses to fetch big map diffs from during initial synchronization"""
        addresses = set()
        for handler_config in self._config.handlers:
            addresses.add(cast(ContractConfig, handler_config.contract).address)
        return addresses

    async def _get_big_map_paths(self) -> Set[str]:
        """Get addresses to fetch big map diffs from during initial synchronization"""
        paths = set()
        for handler_config in self._config.handlers:
            paths.add(handler_config.path)
        return paths


class HeadIndex(Index):
    _config: HeadIndexConfig

    def __init__(self, ctx: DipDupContext, config: HeadIndexConfig, datasource: TzktDatasource) -> None:
        super().__init__(ctx, config, datasource)
        self._queue: Deque[HeadBlockData] = deque()

    async def _synchronize(self, last_level: int, cache: bool = False) -> None:
        self._logger.info('Setting index level to %s and moving on', last_level)
        await self.state.update_status(status=IndexStatus.REALTIME, level=last_level)

    async def _process_queue(self) -> None:
        while self._queue:
            head = self._queue.popleft()
            self._logger.info('Processing head realtime message, %s left in queue', len(self._queue))

            level = head.level
            if level <= self.state.level:
                raise RuntimeError(f'Level of head must be higher than index state level: {level} <= {self.state.level}')

            async with in_global_transaction():
                self._logger.info('Processing head info of level %s', level)
                for handler_config in self._config.handlers:
                    await self._call_matched_handler(handler_config, head)
                await self.state.update_status(level=level)

    async def _call_matched_handler(self, handler_config: HeadHandlerConfig, head: HeadBlockData) -> None:
        if not handler_config.parent:
            raise ConfigInitializationException

        await self._ctx.fire_handler(
            handler_config.callback,
            handler_config.parent.name,
            self.datasource,
            head.hash,
            (head,),
        )

    def push_head(self, head: HeadBlockData) -> None:
        self._queue.append(head)
