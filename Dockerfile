FROM python:3.12-slim

WORKDIR /app

#optional- if we change to OS-level SMB mount 
RUN yum install cifs-utils 

COPY requirements.txt .
RUN pip install -r requirements.txt \ 
    --index-url http://internal-pypi/simple/ \
    --trusted-host internal-pypi

COPY . .

CMD ["python", "app.py"]
