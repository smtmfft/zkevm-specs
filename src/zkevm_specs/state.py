from typing import NamedTuple, Tuple, List, Set, Dict, Optional
from enum import IntEnum
from math import log, ceil

from zkevm_specs.evm.table import MPTProofType

from .util import FQ, RLC, U160, U256, Expression, linear_combine
from .encoding import U8, is_circuit_code
from .evm import (
    RW,
    AccountFieldTag,
    CallContextFieldTag,
    TxLogFieldTag,
    TxReceiptFieldTag,
    MPTTableRow,
    lookup,
)

MAX_RW_COUNTER = 2**32 - 1
MAX_MEMORY_ADDRESS = 2**32 - 1
MAX_KEY_DIFF = 2**32 - 1
MAX_STACK_PTR = 1023
MAX_TAG = 12  # Number of Tag variants
MAX_ID = 2**28 - 1  # Maximum number of calls in a block
MAX_ADDRESS = 2**160 - 1  # Ethereum Address size
MAX_FIELD_TAG = 24  # Max(# of CallContextFieldTag, # of AccountFieldTag) - 1
RW_COUNTER_BITS = ceil(log(MAX_RW_COUNTER + 1, 2))  # 32
TAG_BITS = ceil(log(MAX_TAG + 1, 2))  # 4
ID_BITS = ceil(log(MAX_ID + 1, 2))  # 28
ADDRESS_BITS = ceil(log(MAX_ADDRESS + 1, 2))  # 160
FIELD_TAG_BITS = ceil(log(MAX_FIELD_TAG + 1, 2))  # 5


class Tag(IntEnum):
    """
    Tag used as first key in the State Circuit Rows to "select" the operation target.
    """

    # Start Tag is used both as padding before the rest of the operations and
    # also to discard constraints with the previous row that would fail due to
    # wrapping around with the end of the table.
    Start = 1
    Memory = 2
    Stack = 3
    Storage = 4
    CallContext = 5
    Account = 6
    TxRefund = 7
    TxAccessListAccount = 8
    TxAccessListAccountStorage = 9
    AccountDestructed = 10
    TxLog = 11
    TxReceipt = 12


class Row(NamedTuple):
    """
    State circuit row
    """

    # fmt: off
    rw_counter: FQ
    is_write: FQ # boolean
    # key0 takes the Tag value used to select the constraints of a particular
    # operation; the rest of they keys are used differently for each operation.
    # See the meaning of each key for each operation in ../../specs/tables.md
    # key2 is 160bit Address.  key4 is RLC encoded
    # Key naming:
    # - keys[0]: tag
    # - keys[1]: id
    # - keys[2]: address
    # - keys[3]: field_tag
    # - keys[4]: storage_key
    keys: Tuple[FQ, FQ, FQ, FQ, FQ]
    key2_limbs: Tuple[FQ, FQ, FQ, FQ, FQ, # key2 in Little-Endian (limbs in base 2**16)
                      FQ, FQ, FQ, FQ, FQ]
    key4_bytes: Tuple[FQ,FQ,FQ,FQ,FQ,FQ,FQ,FQ, # key4 in Little-Endian (limbs in base 2**8)
                      FQ,FQ,FQ,FQ,FQ,FQ,FQ,FQ,
                      FQ,FQ,FQ,FQ,FQ,FQ,FQ,FQ,
                      FQ,FQ,FQ,FQ,FQ,FQ,FQ,FQ]
    value: FQ
    committed_value: FQ

    root: FQ

    # fmt: on

    def tag(self):
        return self.keys[0]

    def id(self):
        return self.keys[1]

    def address(self):
        return self.keys[2]

    def address_limbs(self):
        return self.key2_limbs

    def field_tag(self):
        return self.keys[3]

    def storage_key(self):
        return self.keys[4]

    def storage_key_bytes(self):
        return self.key4_bytes


class Tables:
    """
    Tables used for lookup from the state circuit.
    """

    mpt_table: Set[MPTTableRow]

    def __init__(self, mpt_table: Set[MPTTableRow]):
        self.mpt_table = mpt_table

    def mpt_lookup(
        self,
        address: Expression,
        proof_type: Expression,
        storage_key: Expression,
        value: Expression,
        value_prev: Expression,
        root: Expression,
        root_prev: Expression,
    ) -> MPTTableRow:
        query = {
            "address": address,
            "proof_type": proof_type,
            "storage_key": storage_key,
            "value": value,
            "value_prev": value_prev,
            "root": root,
            "root_prev": root_prev,
        }
        return lookup(MPTTableRow, self.mpt_table, query)


# Boolean Expression builder
def all_keys_eq(row: Row, row_prev: Row) -> bool:
    eq = True
    for i in range(len(row.keys)):
        eq = eq and (row.keys[i] == row_prev.keys[i])
    return eq


class LowerThanGadget:
    lhs: List[FQ]  # Little-Endian list of left hand side limbs
    rhs: List[FQ]  # Little-Endian list of right hand side limbs

    def __init__(self, lhs: List[FQ], rhs: List[FQ]):
        self.lhs = lhs
        self.rhs = rhs

    def verify(self):
        lt = self.lhs[0].n < self.rhs[0].n
        for i in range(1, len(self.lhs)):
            lt = self.lhs[i].n < self.rhs[i].n or (self.lhs[i] == self.rhs[i] and lt)
        assert lt


@is_circuit_code
def assert_in_range(x: FQ, min_val: int, max_val: int) -> None:
    assert min_val <= x.n and x.n <= max_val


@is_circuit_code
def check_start(row: Row, row_prev: Row):
    # 1.0. rw_counter is 0
    assert row.rw_counter == 0


@is_circuit_code
def check_memory(row: Row, row_prev: Row):
    get_call_id = lambda row: row.id()
    get_memory_address = lambda row: row.address()

    # 2.0. Unused keys are 0
    assert row.field_tag() == 0
    assert row.storage_key() == 0

    # 2.1. First access for a set of all keys
    #
    # When the set of all keys changes (first access of an address in a call)
    # - If READ, value must be 0
    if not all_keys_eq(row, row_prev) and row.is_write == 0:
        assert row.value == 0

    # 2.2. mem_addr in range
    assert_in_range(get_memory_address(row), 0, MAX_MEMORY_ADDRESS)

    # 2.3. value is a byte
    assert_in_range(row.value, 0, 2**8 - 1)

    # 2.4 state root does not change
    assert row.root == row_prev.root


@is_circuit_code
def check_stack(row: Row, row_prev: Row):
    get_call_id = lambda row: row.id()
    get_stack_ptr = lambda row: row.address()

    # 3.0. Unused keys are 0
    assert row.field_tag() == 0
    assert row.storage_key() == 0

    # 3.1. First access for a set of all keys
    #
    # The first stack operation in a stack position is always a write (can't
    # read if it isn't written before)
    #
    # When the set of all keys changes (first access of a stack position in a call)
    # - It must be a WRITE
    if not all_keys_eq(row, row_prev):
        assert row.is_write == 1

    # 3.2. stack_ptr in range
    stack_ptr = get_stack_ptr(row)
    assert_in_range(stack_ptr, 0, MAX_STACK_PTR)

    # 3.3. stack_ptr only increases by 0 or 1
    if row.tag() == row_prev.tag() and get_call_id(row) == get_call_id(row_prev):
        stack_ptr_diff = get_stack_ptr(row) - get_stack_ptr(row_prev)
        assert_in_range(stack_ptr_diff, 0, 1)

    # 3.4 state root does not change
    assert row.root == row_prev.root


@is_circuit_code
def check_storage(row: Row, row_prev: Row, row_next: Row, tables: Tables):
    # 4.0. Unused keys are 0
    assert row.field_tag() == 0

    is_non_exist = FQ(row.value.expr() == FQ(0)) * FQ(row.committed_value.expr() == FQ(0))

    # 4.1. MPT lookup for last access to (address, storage_key)
    if not all_keys_eq(row, row_next):
        tables.mpt_lookup(
            row.address(),
            is_non_exist * FQ(MPTProofType.NonExistingStorageProof)
            + (1 - is_non_exist) * FQ(MPTProofType.StorageMod),
            row.storage_key(),
            row.value,
            row.committed_value,
            row.root,
            row_prev.root,
        )
    else:
        assert row.root == row_prev.root


@is_circuit_code
def check_call_context(row: Row, row_prev: Row):
    get_call_id = lambda row: row.id()
    get_field_tag = lambda row: row.field_tag()

    # 5.0. Unused keys are 0
    assert row.address() == 0
    assert row.storage_key() == 0

    # 5.1 state root does not change
    assert row.root == row_prev.root

    # 5.2 First access for a set of all keys
    # - If READ, value must be 0
    if not all_keys_eq(row, row_prev) and row.is_write == 0:
        assert row.value == 0


@is_circuit_code
def check_account(row: Row, row_prev: Row, row_next: Row, tables: Tables):
    get_addr = lambda row: row.address()

    field_tag = row.field_tag()
    proof_type = MPTProofType.from_account_field_tag(field_tag)

    # 6.0. Unused keys are 0
    assert row.id() == 0
    assert row.storage_key() == 0

    # 6.2. MPT storage lookup for last access to (address, field_tag)
    if not all_keys_eq(row, row_next):
        tables.mpt_lookup(
            get_addr(row),
            FQ(proof_type),
            row.storage_key(),
            row.value,
            row.committed_value,
            row.root,
            row_prev.root,
        )
    else:
        assert row.root == row_prev.root

    # NOTE: Value transition rules are constrained via the EVM circuit: for example,
    # Nonce only increases by 1 or decreases by 1 (on revert).


@is_circuit_code
def check_tx_refund(row: Row, row_prev: Row):
    get_tx_id = lambda row: row.id()

    # 7.0. Unused keys are 0
    assert row.address() == 0
    assert row.field_tag() == 0
    assert row.storage_key() == 0

    # 7.1 state root does not change
    assert row.root == row_prev.root

    # TODO: Missing constraints
    # - When keys change, value must be 0


@is_circuit_code
def check_tx_access_list_account(row: Row, row_prev: Row):
    get_tx_id = lambda row: row.id()
    get_addr = lambda row: row.address()

    # 9.0. Unused keys are 0
    assert row.field_tag() == 0
    assert row.storage_key() == 0

    # 9.1 state root does not change
    assert row.root == row_prev.root

    # 9.2 First access for a set of all keys
    # - If READ, value must be 0
    if not all_keys_eq(row, row_prev) and row.is_write == 0:
        assert row.value == 0


@is_circuit_code
def check_tx_access_list_account_storage(row: Row, row_prev: Row):
    get_tx_id = lambda row: row.id()
    get_addr = lambda row: row.address()
    get_storage_key = lambda row: row.storage_key()

    # 8.0. Unused keys are 0
    assert row.field_tag() == 0

    # 8.1 State root cannot change
    assert row.root == row_prev.root

    # 8.2 First access for a set of all keys
    # - If READ, value must be 0
    if not all_keys_eq(row, row_prev) and row.is_write == 0:
        assert row.value == 0


@is_circuit_code
def check_account_destructed(row: Row, row_prev: Row):
    get_addr = lambda row: row.address()

    # 10.0. Unused keys are 0
    assert row.id() == 0
    assert row.field_tag() == 0
    assert row.storage_key() == 0

    # TODO: Missing constraints
    # - When keys change, value must be 0

    # TODO: add MPT lookup


@is_circuit_code
def check_tx_log(row: Row, row_prev: Row):
    # tx_id | log_id | field_tag | index | value
    tx_id = row.id()
    prev_tx_id = row_prev.id()

    # 12.0 is_write is always true
    assert row.is_write == 1

    # 12.1 state root does not change
    assert row.root == row_prev.root

    # removed field_tag-specific constraints as issue
    # https://github.com/privacy-scaling-explorations/zkevm-specs/issues/221


@is_circuit_code
def check_tx_receipt(row: Row, row_prev: Row):
    tx_id = row.id()
    pre_tx_id = row_prev.id()
    field_tag = row.field_tag()
    # 11.0. Unused keys are 0
    assert row.address() == 0
    assert row.storage_key() == 0

    # 11.1 value for tag `PostStateOrStatus` is bool (0 or 1) according to EIP#658
    if field_tag == U256(TxReceiptFieldTag.PostStateOrStatus):
        assert row.value in [0, 1]

    # 11.2 when tx id changes, must be increasing by one, the CumulativeGasUsed must be increasing as well
    if tx_id != pre_tx_id and row.tag() == row_prev.tag():
        assert tx_id == pre_tx_id + 1
        if field_tag == U256(TxReceiptFieldTag.CumulativeGasUsed):
            assert row.value.n > row_prev.value.n

    # 11.3 tx id starts with 1
    if row.tag() != row_prev.tag():
        # first row the tx id is 1
        assert tx_id == FQ(1)

    assert_in_range(tx_id, 1, 2**11)

    # 11.4 state root does not change
    assert row.root == row_prev.root


@is_circuit_code
def check_state_row(row: Row, row_prev: Row, row_next: Row, tables: Tables, randomness: FQ):
    #
    # Constraints that affect all rows, no matter which Tag they use
    #

    # 0.0. tag, id, field_tag are in the expected range
    assert_in_range(row.tag(), 1, MAX_TAG)
    assert_in_range(row.id(), 0, MAX_ID)
    # NOTE: In the implementation, the range check of field_tag is applied per
    # target.
    assert_in_range(row.field_tag(), 0, MAX_FIELD_TAG)

    # 0.1. address is linear combination of 10 x 16bit limbs and also in range
    for limb in row.address_limbs():
        assert_in_range(limb, 0, 2**16 - 1)
    assert row.address() == linear_combine(row.address_limbs(), FQ(2**16), range_check=False)

    # 0.2. address is RLC encoded
    assert row.storage_key() == linear_combine(row.storage_key_bytes(), randomness)

    # 0.3. is_write is boolean
    assert row.is_write in [0, 1]

    # 0.4. Keys and RWC are sorted in lexicographic order for same Tag
    #
    # This check also ensures that Tag monotonically increases for all values
    # except for Start
    #
    # When in two consecutive rows the keys are equal in a column:
    # - The corresponding keys in the following column must be increasing.
    #
    # address is RLC encoded, so it doesn't keep the order.  We use the address
    # bytes decomposition instead.  Since we will use a chain of comparison
    # gadgets, we try to merge multiple keys together to reduce the number of
    # required gadgets.

    # NOTE: the current implementation uses the following order: tag,
    # field_tag, id, address, storage_key, rw_counter.  Some constraints of
    # this spec require field_tag to come after id and address, so we keep the
    # spec different from the implementation, and plan to update the
    # implementation to follow the spec in the future.

    assert TAG_BITS + ID_BITS == 2 * 16

    # Return a list of 16 bit limbs with all the keys and rw_counter used for
    # the lexicographic ordering.  The field ordering is (from most significant
    # to less significant):
    # - tag
    # - id
    # - address
    # - field_tag
    # - storage_key
    # - rw_counter
    def keys_rwc_to_limbs_in_order(row: Row) -> List[FQ]:
        v = row.tag().n
        v = v * 2**ID_BITS + row.id().n  # 2 limbs
        v = v * 2**ADDRESS_BITS + row.address().n  # + 10 limbs = 12 limbs
        v = v * 2**16 + row.field_tag().n  # + 1 limb = 13 limbs
        v = v * (2**32) + int.from_bytes(
            map(lambda b: b.n, row.storage_key_bytes()), "little"
        )  # + 16 limbs = 29 limbs
        v = v * 2**RW_COUNTER_BITS + row.rw_counter.n  # + 2 limbs = 31 limbs
        limbs = []
        for i in range(31):
            limbs.append(FQ(v & 0xFFFF))
            v = v >> 16
        return limbs

    limbs_prev = keys_rwc_to_limbs_in_order(row_prev)
    limbs = keys_rwc_to_limbs_in_order(row)
    if row.tag() != Tag.Start:
        LowerThanGadget(limbs_prev, limbs).verify()

    # 0.5. Read consistency
    #
    # When a row is READ
    # AND When all the keys are equal in two consecutive a rows:
    # - The corresponding value must be equal to the previous row
    if row.is_write == 0 and all_keys_eq(row, row_prev):
        assert row.value == row_prev.value

    if all_keys_eq(row, row_prev):
        assert row.committed_value == row_prev.committed_value

    # 8. RWC !=0 except for Tag.Start
    if row.tag() != Tag.Start:
        assert row.rw_counter != 0

    #
    # Constraints specific to each Tag
    #
    if row.tag() == Tag.Start:
        check_start(row, row_prev)
    elif row.tag() == Tag.Memory:
        check_memory(row, row_prev)
    elif row.tag() == Tag.Stack:
        check_stack(row, row_prev)
    elif row.tag() == Tag.Storage:
        check_storage(row, row_prev, row_next, tables)
    elif row.tag() == Tag.CallContext:
        check_call_context(row, row_prev)
    elif row.tag() == Tag.Account:
        check_account(row, row_prev, row_next, tables)
    elif row.tag() == Tag.TxRefund:
        check_tx_refund(row, row_prev)
    elif row.tag() == Tag.TxAccessListAccountStorage:
        check_tx_access_list_account_storage(row, row_prev)
    elif row.tag() == Tag.TxAccessListAccount:
        check_tx_access_list_account(row, row_prev)
    elif row.tag() == Tag.AccountDestructed:
        check_account_destructed(row, row_prev)
    elif row.tag() == Tag.TxReceipt:
        check_tx_receipt(row, row_prev)
    elif row.tag() == Tag.TxLog:
        check_tx_log(row, row_prev)
    else:
        raise ValueError("Unreacheable")


# State circuit operation superclass
class Operation(NamedTuple):
    """
    State circuit operation
    """

    rw_counter: int
    rw: RW
    tag: U256
    id: U256
    address: U256
    field_tag: U256
    storage_key: U256
    value: FQ
    committed_value: FQ


class StartOp(Operation):
    """
    Start Operation
    """

    def __new__(self):
        # fmt: off
        return super().__new__(self, 0, 0,
                U256(Tag.Start), U256(0), U256(0), U256(0), U256(0), # keys
                FQ(0), FQ(0)) # values
        # fmt: on


class MemoryOp(Operation):
    """
    Memory Operation
    """

    # The yellow paper allows memory addresses to have up to 256 bits, but the
    # gas cost for memory operations is quadratic in the maximum memory address
    # touched. From equation 326 in the yellow paper, for C_mem(a), the maximum
    # memory address touched will fit in to 32 bits until the gas limit is over
    # 3.6e16.
    def __new__(self, rw_counter: int, rw: RW, call_id: int, mem_addr: U160, value: U8):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.Memory), U256(call_id), U256(mem_addr), U256(0), U256(0), # keys
                FQ(value), FQ(0)) # values
        # fmt: on


class StackOp(Operation):
    """
    Stack Operation
    """

    def __new__(self, rw_counter: int, rw: RW, call_id: int, stack_ptr: int, value: FQ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.Stack), U256(call_id), U256(stack_ptr), U256(0), U256(0), # keys
                value, FQ(0)) # values
        # fmt: on


class StorageOp(Operation):
    """
    Storage Operation
    """

    def __new__(
        self,
        rw_counter: int,
        rw: RW,
        tx_id: int,
        addr: U160,
        key: U256,
        value: FQ,
        committed_value: FQ,
    ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.Storage), U256(tx_id), U256(addr), U256(0), U256(key), # keys
                value, committed_value) # values
        # fmt: on


class CallContextOp(Operation):
    """
    CallContext Operation
    """

    def __new__(
        self, rw_counter: int, rw: RW, call_id: int, field_tag: CallContextFieldTag, value: FQ
    ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.CallContext), U256(call_id), U256(0), U256(field_tag), U256(0), # keys
                value, FQ(0)) # values
        # fmt: on


class AccountOp(Operation):
    """
    Account Operation
    """

    def __new__(
        self,
        rw_counter: int,
        rw: RW,
        addr: U160,
        field_tag: AccountFieldTag,
        value: FQ,
        committed_value: FQ,
    ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.Account), U256(0), U256(addr), U256(field_tag), U256(0), # keys
                value, committed_value) # values
        # fmt: on


class TxRefundOp(Operation):
    """
    TxRefund Operation
    """

    def __new__(self, rw_counter: int, rw: RW, tx_id: int, value: FQ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.TxRefund), U256(tx_id), U256(0), U256(0), U256(0), # keys
                value, FQ(0)) # values
        # fmt: on


class TxAccessListAccountOp(Operation):
    """
    TxAccessListAccount Operation
    """

    def __new__(self, rw_counter: int, rw: RW, tx_id: int, addr: U160, value: FQ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.TxAccessListAccount), U256(tx_id), U256(addr), U256(0), U256(0), # keys
                value, FQ(0)) # values
        # fmt: on


class TxAccessListAccountStorageOp(Operation):
    """
    TxAccessListAccountStorage Operation
    """

    def __new__(self, rw_counter: int, rw: RW, tx_id: int, addr: U160, key: U256, value: FQ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.TxAccessListAccountStorage),
                U256(tx_id), U256(addr), U256(0), U256(key), # keys
                value, FQ(0)) # values
        # fmt: on


class AccountDestructedOp(Operation):
    """
    AccountDestructed Operation
    """

    def __new__(self, rw_counter: int, rw: RW, addr: U160, value: FQ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.AccountDestructed), U256(0), U256(addr), U256(0), U256(0), # keys
                value, FQ(0)) # values
        # fmt: on


class TxLogOp(Operation):
    """
    TxLog Operation
    """

    def __new__(
        self,
        rw_counter: int,
        rw: RW,
        tx_id: int,
        log_id: int,
        field_tag: TxLogFieldTag,
        index: int,
        value: FQ,
    ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.TxLog),U256(tx_id), U256(log_id), U256(field_tag), U256(index), # keys
                value, FQ(0)) # values
        # fmt: on


class TxReceiptOp(Operation):
    """
    TxReceipt Operation
    """

    def __new__(self, rw_counter: int, rw: RW, tx_id: int, field_tag: TxReceiptFieldTag, value: FQ):
        # fmt: off
        return super().__new__(self, rw_counter, rw,
                U256(Tag.TxReceipt), U256(tx_id),  U256(0), U256(field_tag), U256(0), # keys
                value, FQ(0)) # values
        # fmt: on


def op2row(
    op: Operation,
    randomness: FQ,
    root: FQ,
) -> Row:
    rw_counter = FQ(op.rw_counter)
    is_write = FQ(0) if op.rw == RW.Read else FQ(1)
    tag = FQ(op.tag)
    id = FQ(op.id)
    address = FQ(op.address)
    address_bytes = op.address.to_bytes(20, "little")
    address_limbs = tuple(
        [FQ(address_bytes[i] + 2**8 * address_bytes[i + 1]) for i in range(0, 20, 2)]
    )
    field_tag = FQ(op.field_tag)
    storage_key_rlc = RLC(op.storage_key, randomness)
    storage_key = storage_key_rlc.expr()
    storage_key_bytes = tuple([FQ(x) for x in storage_key_rlc.le_bytes])

    keys = (tag, id, address, field_tag, storage_key)

    value = FQ(op.value)
    committed_value = FQ(op.committed_value)

    return Row(
        rw_counter,
        is_write,
        keys,
        address_limbs,  # type: ignore
        storage_key_bytes,  # type: ignore
        value,
        committed_value,
        root,
    )


# Generate the advice Rows from a list of Operations
def assign_state_circuit(ops: List[Operation], randomness: FQ) -> List[Row]:
    mpt_updates = _mock_mpt_updates(ops, randomness)

    # MPT keys for each Storage and Account row, and None otherwise.
    mpt_keys = [_mpt_key(op) for op in ops]
    # MPT updates for each Storage and Account row, and None otherwise.
    updates = [None if key is None else mpt_updates.get(key) for key in mpt_keys]
    # root_prev for each Storage and Account row, and None otherwise.
    roots = [None if update is None else update.root_prev.expr() for update in updates]

    # With real mpt updates, the final root would be obtained from the public
    # input. For _mock_mpt_updates, it's just 3 + 5 * number of MPT updates.
    final_root = FQ(3 + 5 * len(mpt_updates))
    roots.append(final_root)

    # Fill in the None roots with the first non-None value that comes after it.
    root: FQ = final_root
    for i in reversed(range(len(roots))):
        maybe_root = roots[i]
        if maybe_root is None:
            roots[i] = root
        else:
            root = maybe_root

    rows = []
    for op, maybe_root in zip(ops, roots[1:]):
        assert maybe_root is not None
        rows.append(op2row(op, randomness, maybe_root))
    return rows


def mpt_table_from_ops(ops: List[Operation], randomness: FQ) -> Set[MPTTableRow]:
    return set(_mock_mpt_updates(ops, randomness).values())


def _mpt_key(op: Operation) -> Optional[Tuple[FQ, FQ, FQ]]:
    if op.tag != Tag.Account and op.tag != Tag.Storage:
        return None
    return (FQ(op.address), FQ(op.field_tag), FQ(op.storage_key))


def _mock_mpt_updates(ops: List[Operation], randomness: FQ) -> Dict[Tuple[FQ, FQ, FQ], MPTTableRow]:
    # makes fake mpt updates for a list of rows. the state root starts at 5 and
    # is incremented by 3 for each Account or Storage MPT update.
    mpt_map = {}

    root = 3
    for op in ops:
        mpt_key = _mpt_key(op)
        if mpt_key is None or mpt_key in mpt_map:
            continue

        field_tag = op.field_tag
        proof_type = MPTProofType.StorageMod  # type warning if None
        if isinstance(field_tag, AccountFieldTag):
            proof_type = MPTProofType.from_account_field_tag(field_tag)

        new_root = root + 5
        mpt_map[mpt_key] = MPTTableRow(
            FQ(op.address),
            FQ(proof_type),
            RLC(op.storage_key, randomness).expr(),
            FQ(new_root),
            FQ(root),
            op.value,
            op.committed_value,
        )
        root = new_root

    return mpt_map
