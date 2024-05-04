# Copyright (c) ONNX Project Contributors
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys
import typing
from typing import Any, Sequence

import numpy as np

import onnx
import onnx.external_data_helper
from onnx import helper, subbyte

if typing.TYPE_CHECKING:
    import numpy.typing as npt


def combine_pairs_to_complex(fa: Sequence[int]) -> list[complex]:
    """Converts alternating [real, imaginary, ...] numbers to complex numbers."""
    return [complex(fa[i * 2], fa[i * 2 + 1]) for i in range(len(fa) // 2)]


def _left_shift_16_bits(
    data: npt.NDArray[np.uint16 | np.uint32],
) -> npt.NDArray[np.uint32]:
    # The left shifted result is always int64, so we need to convert it back to uint32
    return (data << 16).astype(np.uint32)


def bfloat16_to_float32(
    data: npt.NDArray[np.uint16 | np.uint32],
) -> npt.NDArray[np.float32]:
    """Converts ndarray of bf16 (as uint16 / uint32) to f32.

    Args:
        data: A numpy array, empty dimensions are allowed if dims is
            None.

    Returns:
        A numpy array of float32 with the same dimension.
    """
    return _left_shift_16_bits(data).view(np.float32)


def _float8e4m3fn_to_float32_scalar(ival: np.integer, uz: bool) -> np.float32:
    """Converts a single float8e4m3 value to float.

    Args:
        ival: The binary representation of the float8e4m3 value, as int.
        uz: Unique zero. No negative zero or negative inf.
    """
    range_min = 0b0_0000_000  # 0x00, 0
    range_max = 0b1_1111_111  # 0xFF, 255
    fn_uz_nan = 0b1_0000_000  # 0x80, 128
    fn_nan = 0b0_1111_111  # 0x7F, 127
    if ival < range_min or ival > range_max:
        raise ValueError(
            f"{ival} is not a float8 value because its binary representation is out of range [0, 255]."
        )
    if uz:
        exponent_bias = 8
        # Only positive NaN is defined
        if ival == fn_uz_nan:
            return np.float32(np.nan)
    else:
        exponent_bias = 7
        # Both positive and negative NaN are defined
        if ival == range_max:
            return np.float32(-np.nan)
        if ival == fn_nan:
            return np.float32(np.nan)

    # Mask out the sign, exponent and mantissa parts
    sign_mask = 0b1_0000_000  # First bit is the sign bit
    sign = ival & sign_mask
    exponent_mask = 0b0_1111_000  # The next 4 bits are the exponent

    exponent = (ival & exponent_mask) >> 3
    mantissa_mask = 0b0_0000_111  # The last 3 bits are the mantissa
    mantissa = ival & mantissa_mask

    # Construct the float32 value
    # First move the sign bit to the correct position
    result = sign << 24
    if exponent == 0:
        # Subnormal number
        if mantissa > 0:
            # TODO: Explain this process
            exponent = 127 - exponent_bias
            if mantissa & 0b100 == 0:
                mantissa &= 0b011
                mantissa <<= 1
                exponent -= 1
            if mantissa & 0b100 == 0:
                mantissa &= 0b011
                mantissa <<= 1
                exponent -= 1
            result |= (mantissa & 0b011) << 21
            result |= exponent << 23
    else:
        # Normal number
        # float32: e8m23
        # []_[][][][][][][][]_[][][][][][][][][][][][][][][][][][][][][][][]
        # 31   29  27  25  23 22  20  18  16  14  12  10 9 8 7 6 5 4 3 2 1 0
        # S   0 0 0 0 E E E E  M M M 0 ....................................0
        result |= mantissa << 20
        exponent += 127 - exponent_bias
        result |= exponent << 23
    return np.uint32(result).view(np.float32)


_float8e4m3fn_to_float32 = np.vectorize(
    _float8e4m3fn_to_float32_scalar, excluded=("uz",)
)


def float8e4m3_to_float32(
    data: np.int16 | np.int32 | np.ndarray,
    fn: bool = True,
    uz: bool = False,
) -> np.ndarray:
    """Converts ndarray of float8e4m3 (as uint) to float32.

    See :ref:`onnx-detail-float8` for technical details.

    Args:
        data: A numpy array, empty dimensions are allowed if dims is None.
        fn: Finite. No infinite values.
        uz: Unique zero. No negative zero or negative inf.

    Returns:
        A numpy array of converted float32.
    """
    if not fn:
        raise NotImplementedError(
            "float32_to_float8e4m3 not implemented with fn=False."
        )
    return _float8e4m3fn_to_float32(data, fn=fn, uz=uz)


def _float8e5m2_to_float32_scalar(ival: int, fn: bool, uz: bool) -> np.float32:
    """Converts a single float8e5m2 value to float.

    Args:
        ival: The binary representation of the float8e5m2 value, as int.
        fn: Finite. No infinite values.
        uz: Unique zero. No negative zero or negative inf.
    """
    range_min = 0b0_0000_000  # 0x00, 0
    range_max = 0b1_1111_111  # 0xFF, 255
    if ival < range_min or ival > range_max:
        raise ValueError(
            f"{ival} is not a float8 value because its binary representation is out of range [0, 255]."
        )
    if fn and uz:
        fn_uz_nan = 0b1_00000_00  # 0x80, 128
        if ival == fn_uz_nan:
            return np.float32(np.nan)
        exponent_bias = 16
    elif not fn and not uz:
        negative_nan = 0b1_11111_01
        if ival >= negative_nan:
            # This includes 0b1_11111_01, 0b1_11111_10, 0b1_11111_11
            return np.float32(-np.nan)

        positive_nan_min = 0b1_1111_01
        positive_nan_max = 0b1_1111_11
        if positive_nan_min <= ival <= positive_nan_max:
            return np.float32(np.nan)

        negative_inf = 0b1_11111_00
        if ival == negative_inf:
            return np.float32(-np.inf)
        positive_inf = 0b0_11111_00
        if ival == positive_inf:
            return np.float32(np.inf)
        exponent_bias = 15
    else:
        raise NotImplementedError("fn and uz must be both False or True.")

    sign_mask = 0b1_00000_00
    exponent_mask = 0b0_11111_00
    mantissa_mask = 0b0_00000_11

    sign = ival & sign_mask  # First bit is the sign bit
    exponent = (ival & exponent_mask) >> 2  # The next 5 bits are the exponent
    mantissa = ival & mantissa_mask  # The last 2 bits are the mantissa

    # Construct the float32 value
    # First move the sign bit to the correct position
    result = sign << 24
    if exponent == 0:
        # Subnormal number
        if mantissa > 0:
            exponent = 127 - exponent_bias
            if mantissa & 0b10 == 0:
                mantissa &= 0b01
                mantissa <<= 1
                exponent -= 1
            result |= (mantissa & 0b01) << 22
            result |= exponent << 23
    else:
        # Normal number
        # float32: e8m23
        # []_[][][][][][][][]_[][][][][][][][][][][][][][][][][][][][][][][]
        # 31   29  27  25  23 22  20  18  16  14  12  10 9 8 7 6 5 4 3 2 1 0
        # S   0 0 0 E E E E E  M M 0 ......................................0
        result |= mantissa << 21
        exponent += 127 - exponent_bias
        result |= exponent << 23
    f = np.uint32(result).view(np.float32)
    return f


_float8e5m2_to_float32 = np.vectorize(
    _float8e5m2_to_float32_scalar, excluded=["fn", "uz"]
)


def float8e5m2_to_float32(
    data: np.int16 | np.int32 | np.ndarray,
    fn: bool = False,
    uz: bool = False,
) -> np.ndarray:
    """Converts ndarray of float8, e5m2 (as uint32) to f32 (as uint32).

    See :ref:`onnx-detail-float8` for technical details.

    Args:
        data: A numpy array, empty dimensions are allowed if dims is None.
        fn: Finite. No infinite values.
        uz: Unique zero. No negative zero or negative inf.

    Returns:
        A numpy array of converted float32.
    """
    return _float8e5m2_to_float32(data, fn=fn, uz=uz)


def to_array(
    tensor: onnx.TensorProto, base_dir: str = ""
) -> np.ndarray:  # noqa: PLR0911
    """Converts a tensor def object to a numpy array.

    Args:
        tensor: a TensorProto object.
        base_dir: if external tensor exists, base_dir can help to find the path to it

    Returns:
        arr: the converted array.
    """
    if tensor.HasField("segment"):
        raise ValueError("Currently not supporting loading segments.")
    if tensor.data_type == onnx.TensorProto.UNDEFINED:
        raise TypeError("The element type in the input tensor is not defined.")

    tensor_dtype = tensor.data_type
    np_dtype = helper.tensor_dtype_to_np_dtype(tensor_dtype)
    storage_np_dtype = helper.tensor_dtype_to_np_dtype(
        helper.tensor_dtype_to_storage_tensor_dtype(tensor_dtype)
    )
    storage_field = helper.tensor_dtype_to_field(tensor_dtype)
    dims = tensor.dims

    if tensor.data_type == onnx.TensorProto.STRING:
        utf8_strings = getattr(tensor, storage_field)
        ss = [s.decode("utf-8") for s in utf8_strings]
        return np.asarray(ss).astype(np_dtype).reshape(dims)

    # Load raw data from external tensor if it exists
    if onnx.external_data_helper.uses_external_data(tensor):
        onnx.external_data_helper.load_external_data_for_tensor(tensor, base_dir)

    if tensor.HasField("raw_data"):
        # Raw_bytes support: using frombuffer.
        raw_data = tensor.raw_data
        if sys.byteorder == "big":
            # Convert endian from little to big
            raw_data = np.frombuffer(raw_data, dtype=np_dtype).byteswap().tobytes()

        # manually convert bf16 since there's no numpy support
        if tensor_dtype == onnx.TensorProto.BFLOAT16:
            data = np.frombuffer(raw_data, dtype=np.uint16)
            return bfloat16_to_float32(data, dims)

        if tensor_dtype == onnx.TensorProto.FLOAT8E4M3FN:
            data = np.frombuffer(raw_data, dtype=np.uint8)
            return float8e4m3_to_float32(data, dims)

        if tensor_dtype == onnx.TensorProto.FLOAT8E4M3FNUZ:
            data = np.frombuffer(raw_data, dtype=np.uint8)
            return float8e4m3_to_float32(data, dims, uz=True)

        if tensor_dtype == onnx.TensorProto.FLOAT8E5M2:
            data = np.frombuffer(raw_data, dtype=np.uint8)
            return float8e5m2_to_float32(data, dims)

        if tensor_dtype == onnx.TensorProto.FLOAT8E5M2FNUZ:
            data = np.frombuffer(raw_data, dtype=np.uint8)
            return float8e5m2_to_float32(data, dims, fn=True, uz=True)

        if tensor_dtype == onnx.TensorProto.UINT4:
            data = np.frombuffer(raw_data, dtype=np.uint8)
            return unpack_int4(data, dims, signed=False)

        if tensor_dtype == onnx.TensorProto.INT4:
            data = np.frombuffer(raw_data, dtype=np.int8)
            return unpack_int4(data, dims, signed=True)

        return np.frombuffer(raw_data, dtype=np_dtype).reshape(dims)  # type: ignore[no-any-return]

    # float16 is stored as int32 (uint16 type); Need view to get the original value
    if tensor_dtype == onnx.TensorProto.FLOAT16:
        return (
            np.asarray(tensor.int32_data, dtype=np.uint16)
            .reshape(dims)
            .view(np.float16)
        )

    # bfloat16 is stored as int32 (uint16 type); no numpy support for bf16
    if tensor_dtype == onnx.TensorProto.BFLOAT16:
        data = np.asarray(tensor.int32_data, dtype=np.int32)
        return bfloat16_to_float32(data, dims)

    if tensor_dtype == onnx.TensorProto.FLOAT8E4M3FN:
        data = np.asarray(tensor.int32_data, dtype=np.int32)
        return float8e4m3_to_float32(data, dims)

    if tensor_dtype == onnx.TensorProto.FLOAT8E4M3FNUZ:
        data = np.asarray(tensor.int32_data, dtype=np.int32)
        return float8e4m3_to_float32(data, dims, uz=True)

    if tensor_dtype == onnx.TensorProto.FLOAT8E5M2:
        data = np.asarray(tensor.int32_data, dtype=np.int32)
        return float8e5m2_to_float32(data, dims)

    if tensor_dtype == onnx.TensorProto.FLOAT8E5M2FNUZ:
        data = np.asarray(tensor.int32_data, dtype=np.int32)
        return float8e5m2_to_float32(data, dims, fn=True, uz=True)

    if tensor_dtype == onnx.TensorProto.UINT4:
        data = np.asarray(tensor.int32_data, dtype=storage_np_dtype)
        return unpack_int4(data, dims, signed=False)

    if tensor_dtype == onnx.TensorProto.INT4:
        data = np.asarray(tensor.int32_data, dtype=storage_np_dtype)
        return unpack_int4(data, dims, signed=True)

    data = getattr(tensor, storage_field)
    if tensor_dtype in (onnx.TensorProto.COMPLEX64, onnx.TensorProto.COMPLEX128):
        data = combine_pairs_to_complex(data)  # type: ignore[assignment,arg-type]
        return np.asarray(data).astype(np_dtype).reshape(dims)

    return np.asarray(data, dtype=storage_np_dtype).astype(np_dtype).reshape(dims)


def from_array(arr: np.ndarray, name: str | None = None) -> onnx.TensorProto:
    """Converts a numpy array to a tensor def.

    Args:
        arr: a numpy array.
        name: (optional) the name of the tensor.

    Returns:
        TensorProto: the converted tensor def.
    """
    if not isinstance(arr, (np.ndarray, np.generic)):
        raise TypeError(
            f"arr must be of type np.generic or np.ndarray, got {type(arr)}"
        )

    tensor = onnx.TensorProto()
    tensor.dims.extend(arr.shape)
    if name:
        tensor.name = name

    if arr.dtype == object:
        # Special care for strings.
        tensor.data_type = helper.np_dtype_to_tensor_dtype(arr.dtype)
        # TODO: Introduce full string support.
        # We flatten the array in case there are 2-D arrays are specified
        # We throw the error below if we have a 3-D array or some kind of other
        # object. If you want more complex shapes then follow the below instructions.
        # Unlike other types where the shape is automatically inferred from
        # nested arrays of values, the only reliable way now to feed strings
        # is to put them into a flat array then specify type astype(object)
        # (otherwise all strings may have different types depending on their length)
        # and then specify shape .reshape([x, y, z])
        flat_array = arr.flatten()
        for e in flat_array:
            if isinstance(e, str):
                tensor.string_data.append(e.encode("utf-8"))
            elif isinstance(e, np.ndarray):
                for s in e:
                    if isinstance(s, str):
                        tensor.string_data.append(s.encode("utf-8"))
                    elif isinstance(s, bytes):
                        tensor.string_data.append(s)
            elif isinstance(e, bytes):
                tensor.string_data.append(e)
            else:
                raise NotImplementedError(
                    "Unrecognized object in the object array, expect a string, or array of bytes: ",
                    str(type(e)),
                )
        return tensor

    # For numerical types, directly use numpy raw bytes.
    try:
        dtype = helper.np_dtype_to_tensor_dtype(arr.dtype)
    except KeyError as e:
        raise RuntimeError(f"Numpy data type not understood yet: {arr.dtype!r}") from e
    tensor.data_type = dtype
    tensor.raw_data = arr.tobytes()  # note: tobytes() is only after 1.9.
    if sys.byteorder == "big":
        # Convert endian from big to little
        convert_endian(tensor)

    return tensor


def to_list(sequence: onnx.SequenceProto) -> list[Any]:
    """Converts a sequence def to a Python list.

    Args:
        sequence: a SequenceProto object.

    Returns:
        list: the converted list.
    """
    elem_type = sequence.elem_type
    if elem_type == onnx.SequenceProto.TENSOR:
        return [to_array(v) for v in sequence.tensor_values]  # type: ignore[arg-type]
    if elem_type == onnx.SequenceProto.SPARSE_TENSOR:
        return [to_array(v) for v in sequence.sparse_tensor_values]  # type: ignore[arg-type]
    if elem_type == onnx.SequenceProto.SEQUENCE:
        return [to_list(v) for v in sequence.sequence_values]
    if elem_type == onnx.SequenceProto.MAP:
        return [to_dict(v) for v in sequence.map_values]
    raise TypeError("The element type in the input sequence is not supported.")


def from_list(
    lst: list[Any], name: str | None = None, dtype: int | None = None
) -> onnx.SequenceProto:
    """Converts a list into a sequence def.

    Args:
        lst: a Python list
        name: (optional) the name of the sequence.
        dtype: (optional) type of element in the input list, used for specifying
                          sequence values when converting an empty list.

    Returns:
        SequenceProto: the converted sequence def.
    """
    sequence = onnx.SequenceProto()
    if name:
        sequence.name = name

    if dtype:
        elem_type = dtype
    elif len(lst) > 0:
        first_elem = lst[0]
        if isinstance(first_elem, dict):
            elem_type = onnx.SequenceProto.MAP
        elif isinstance(first_elem, list):
            elem_type = onnx.SequenceProto.SEQUENCE
        else:
            elem_type = onnx.SequenceProto.TENSOR
    else:
        # if empty input list and no dtype specified
        # choose sequence of tensors on default
        elem_type = onnx.SequenceProto.TENSOR
    sequence.elem_type = elem_type

    if (len(lst) > 0) and not all(isinstance(elem, type(lst[0])) for elem in lst):
        raise TypeError(
            "The element type in the input list is not the same "
            "for all elements and therefore is not supported as a sequence."
        )

    if elem_type == onnx.SequenceProto.TENSOR:
        for tensor in lst:
            sequence.tensor_values.extend([from_array(tensor)])
    elif elem_type == onnx.SequenceProto.SEQUENCE:
        for seq in lst:
            sequence.sequence_values.extend([from_list(seq)])
    elif elem_type == onnx.SequenceProto.MAP:
        for mapping in lst:
            sequence.map_values.extend([from_dict(mapping)])
    else:
        raise TypeError(
            "The element type in the input list is not a tensor, "
            "sequence, or map and is not supported."
        )
    return sequence


def to_dict(map_proto: onnx.MapProto) -> dict[Any, Any]:
    """Converts a map def to a Python dictionary.

    Args:
        map_proto: a MapProto object.

    Returns:
        The converted dictionary.
    """
    key_list: list[Any] = []
    if map_proto.key_type == onnx.TensorProto.STRING:
        key_list = list(map_proto.string_keys)
    else:
        key_list = list(map_proto.keys)

    value_list = to_list(map_proto.values)
    if len(key_list) != len(value_list):
        raise IndexError(
            "Length of keys and values for MapProto (map name: ",
            map_proto.name,
            ") are not the same.",
        )
    dictionary = dict(zip(key_list, value_list))
    return dictionary


def from_dict(dict_: dict[Any, Any], name: str | None = None) -> onnx.MapProto:
    """Converts a Python dictionary into a map def.

    Args:
        dict_: Python dictionary
        name: (optional) the name of the map.

    Returns:
        MapProto: the converted map def.
    """
    map_proto = onnx.MapProto()
    if name:
        map_proto.name = name
    keys = list(dict_)
    raw_key_type = np.result_type(keys[0])
    key_type = helper.np_dtype_to_tensor_dtype(raw_key_type)

    valid_key_int_types = [
        onnx.TensorProto.INT8,
        onnx.TensorProto.INT16,
        onnx.TensorProto.INT32,
        onnx.TensorProto.INT64,
        onnx.TensorProto.UINT8,
        onnx.TensorProto.UINT16,
        onnx.TensorProto.UINT32,
        onnx.TensorProto.UINT64,
    ]

    if not (
        all(
            np.result_type(key) == raw_key_type  # type: ignore[arg-type]
            for key in keys
        )
    ):
        raise TypeError(
            "The key type in the input dictionary is not the same "
            "for all keys and therefore is not valid as a map."
        )

    values = list(dict_.values())
    raw_value_type = np.result_type(values[0])
    if not all(np.result_type(val) == raw_value_type for val in values):
        raise TypeError(
            "The value type in the input dictionary is not the same "
            "for all values and therefore is not valid as a map."
        )

    value_seq = from_list(values)

    map_proto.key_type = key_type
    if key_type == onnx.TensorProto.STRING:
        map_proto.string_keys.extend(keys)
    elif key_type in valid_key_int_types:
        map_proto.keys.extend(keys)
    map_proto.values.CopyFrom(value_seq)
    return map_proto


def to_optional(optional: onnx.OptionalProto) -> Any | None:
    """Converts an optional def to a Python optional.

    Args:
        optional: an OptionalProto object.

    Returns:
        opt: the converted optional.
    """
    elem_type = optional.elem_type
    if elem_type == onnx.OptionalProto.UNDEFINED:
        return None
    if elem_type == onnx.OptionalProto.TENSOR:
        return to_array(optional.tensor_value)
    if elem_type == onnx.OptionalProto.SPARSE_TENSOR:
        return to_array(optional.sparse_tensor_value)  # type: ignore[arg-type]
    if elem_type == onnx.OptionalProto.SEQUENCE:
        return to_list(optional.sequence_value)
    if elem_type == onnx.OptionalProto.MAP:
        return to_dict(optional.map_value)
    if elem_type == onnx.OptionalProto.OPTIONAL:
        return to_optional(optional.optional_value)
    raise TypeError("The element type in the input optional is not supported.")


def from_optional(
    opt: Any | None, name: str | None = None, dtype: int | None = None
) -> onnx.OptionalProto:
    """Converts an optional value into a Optional def.

    Args:
        opt: a Python optional
        name: (optional) the name of the optional.
        dtype: (optional) type of element in the input, used for specifying
                          optional values when converting empty none. dtype must
                          be a valid OptionalProto.DataType value

    Returns:
        optional: the converted optional def.
    """
    # TODO: create a map and replace conditional branches
    optional = onnx.OptionalProto()
    if name:
        optional.name = name

    if dtype:
        # dtype must be a valid onnx.OptionalProto.DataType
        valid_dtypes = list(onnx.OptionalProto.DataType.values())
        if dtype not in valid_dtypes:
            raise TypeError(f"{dtype} must be a valid onnx.OptionalProto.DataType.")
        elem_type = dtype
    elif isinstance(opt, dict):
        elem_type = onnx.OptionalProto.MAP
    elif isinstance(opt, list):
        elem_type = onnx.OptionalProto.SEQUENCE
    elif opt is None:
        elem_type = onnx.OptionalProto.UNDEFINED
    else:
        elem_type = onnx.OptionalProto.TENSOR

    optional.elem_type = elem_type

    if opt is not None:
        if elem_type == onnx.OptionalProto.TENSOR:
            optional.tensor_value.CopyFrom(from_array(opt))
        elif elem_type == onnx.OptionalProto.SEQUENCE:
            optional.sequence_value.CopyFrom(from_list(opt))
        elif elem_type == onnx.OptionalProto.MAP:
            optional.map_value.CopyFrom(from_dict(opt))
        else:
            raise TypeError(
                "The element type in the input is not a tensor, "
                "sequence, or map and is not supported."
            )
    return optional


def convert_endian(tensor: onnx.TensorProto) -> None:
    """Call to convert endianness of raw data in tensor.

    Args:
        tensor: TensorProto to be converted.
    """
    tensor_dtype = tensor.data_type
    np_dtype = helper.tensor_dtype_to_np_dtype(tensor_dtype)
    tensor.raw_data = (
        np.frombuffer(tensor.raw_data, dtype=np_dtype).byteswap().tobytes()
    )


def create_random_int(
    input_shape: tuple[int], dtype: np.dtype, seed: int = 1
) -> np.ndarray:
    """Create random integer array for backend/test/case/node.

    Args:
        input_shape: The shape for the returned integer array.
        dtype: The NumPy data type for the returned integer array.
        seed: The seed for np.random.

    Returns:
        np.ndarray: Random integer array.
    """
    np.random.seed(seed)
    if dtype in (
        np.uint8,
        np.uint16,
        np.uint32,
        np.uint64,
        np.int8,
        np.int16,
        np.int32,
        np.int64,
    ):
        # the range of np.random.randint is int32; set a fixed boundary if overflow
        end = min(np.iinfo(dtype).max, np.iinfo(np.int32).max)
        start = max(np.iinfo(dtype).min, np.iinfo(np.int32).min)
        return np.random.randint(start, end, size=input_shape).astype(dtype)
    else:
        raise TypeError(f"{dtype} is not supported by create_random_int.")
