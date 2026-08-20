import pandas as pd

dataset = {
    "name":['john','smith','peter'],
    'age':[22,21,23]
}

df = pd.DataFrame(dataset,index=['row1','row2','row3'])

print(df)

 
 