import xgboost as xgb
from onnxmltools.convert import convert_xgboost
from onnxmltools.convert.common.data_types import FloatTensorType

model = xgb.Booster()
model.load_model(
    "C:/Users/fansuri/Documents/pro/fyp/checkpoints_fyp(vessel_forecast)/xgb.json")

initial_type = [('input', FloatTensorType([None, 15]))]
onnx_model = convert_xgboost(model, initial_types=initial_type)

with open("xgb_model.onnx", "wb") as f:
    f.write(onnx_model.SerializeToString())