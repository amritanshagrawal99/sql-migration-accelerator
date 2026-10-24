SET NOCOUNT ON;
GO

IF OBJECT_ID('tempdb..#open_orders') IS NOT NULL DROP TABLE #open_orders;

SELECT o.Id, o.CustomerId, SUM(l.Qty * l.Price) AS Total
INTO #open_orders
FROM dbo.Orders o WITH (NOLOCK)
JOIN dbo.OrderLines l WITH (NOLOCK) ON l.OrderId = o.Id
WHERE o.Status = 'Open'
GROUP BY o.Id, o.CustomerId;

UPDATE o
SET o.Total = t.Total, o.UpdatedAt = GETDATE()
FROM dbo.Orders o
JOIN #open_orders t ON t.Id = o.Id;

DELETE c
FROM dbo.Carts c
JOIN dbo.Orders o ON o.CartId = c.Id
WHERE o.Status = 'Closed';

MERGE dbo.CustomerTotals AS tgt
USING (SELECT CustomerId, SUM(Total) AS Total FROM #open_orders GROUP BY CustomerId) AS src
ON tgt.CustomerId = src.CustomerId
WHEN MATCHED THEN UPDATE SET Total = src.Total
WHEN NOT MATCHED BY TARGET THEN INSERT (CustomerId, Total) VALUES (src.CustomerId, src.Total)
WHEN NOT MATCHED BY SOURCE THEN DELETE;

EXEC dbo.usp_RefreshDashboards @Region = 'IN';
GO
